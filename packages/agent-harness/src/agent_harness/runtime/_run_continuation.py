"""Checkpoint continuation 与 executor approval 恢复协调。"""

from __future__ import annotations

from typing import Any, cast

from agent_harness.events import CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.runtime._orchestrator_base import OrchestratorState
from agent_harness.runtime._orchestrator_support import run_correlation
from agent_harness.runtime.checkpoints import ResumeToken
from agent_harness.runtime.continuation import (
    InvalidRunTransition,
    approval_checkpoint_state,
    checkpoint_identity,
    optional_state_text,
    resume_token_value,
    validate_approval_grant,
    validate_terminal_execution_result,
)
from agent_harness.runtime.evidence import publish_terminal_evidence
from agent_harness.runtime.executor import (
    AgentExecutionContext,
    AgentExecutionLeaseLost,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionUncertain,
    ApprovalGrant,
    RunResult,
    build_execution_context,
)
from agent_harness.runtime.state import TERMINAL_STATUSES, RunStatus
from agent_harness.security.redaction import redact_secrets


class RunContinuation(OrchestratorState):
    """封装 token 校验、approval 恢复与 executor 结果归并。"""

    async def _validate_persisted_approval_grant(self, grant: ApprovalGrant) -> None:
        """把公开 grant 绑定到仍可执行的 durable resolution lease。"""

        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(grant.approval_id)
        if (
            lease is None
            or lease.lease_id != grant.lease_id
            or lease.state not in {"claimed", "execution_owned", "recovery_pending"}
            or lease.approval.status != "waiting"
        ):
            raise InvalidRunTransition("approval grant does not match an active resolution lease")
        approval = lease.approval
        persisted = {
            "tenant_id": approval.tenant_id,
            "identity_id": str(approval.metadata.get("identity_id") or approval.requested_by),
            "agent_id": approval.agent_id,
            "run_id": approval.run_id,
            "action": approval.action,
            "resource": approval.resource,
            "arguments_hash": str(approval.metadata.get("arguments_hash") or ""),
        }
        actual = {
            "tenant_id": grant.tenant_id,
            "identity_id": grant.identity_id,
            "agent_id": grant.agent_id,
            "run_id": grant.run_id,
            "action": grant.action,
            "resource": grant.resource,
            "arguments_hash": grant.arguments_hash,
        }
        mismatch = next(
            (field for field, value in persisted.items() if actual[field] != value),
            None,
        )
        if mismatch is not None:
            raise InvalidRunTransition(f"approval grant persistence mismatch: {mismatch}")

    async def resume_run(
        self,
        resume_token: ResumeToken | str,
        *,
        expected_run_id: str | None = None,
        identity: IdentityContext | None = None,
        approval_grant: ApprovalGrant | None = None,
        defer_terminal: bool = False,
    ) -> RunResult:
        """用 resume token 完成等待中的 run，并校验 URL 与 token 归属。"""

        active_identity = identity or self._identity
        token_value = resume_token_value(resume_token)
        async with self._storage.uow() as uow:
            checkpoint = await uow.checkpoints.get_by_resume_token(token_value)
            if checkpoint is None or checkpoint.tenant_id != active_identity.tenant_id:
                raise LookupError("checkpoint not found")
            if expected_run_id is not None and checkpoint.run_id != expected_run_id:
                raise LookupError("resume token does not belong to run")
            run = await uow.runs.get(checkpoint.run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {checkpoint.run_id}")
        state = checkpoint.state
        checkpoint_kind = state.get("kind")
        is_approval_checkpoint = checkpoint_kind in {
            "agent_executor_approval",
            "policy_approval",
        }
        if not is_approval_checkpoint:
            if checkpoint_kind == "delegation_terminal":
                execution_identity = checkpoint_identity(state)
                terminal_status = state.get("terminal_status")
                persisted_status = RunStatus(run.status)
                if persisted_status in TERMINAL_STATUSES:
                    if terminal_status != persisted_status.value:
                        raise InvalidRunTransition(
                            "delegation terminal checkpoint status is inconsistent"
                        )
                    # terminal 已发布、approval 补偿随后失败时，child final 重试
                    # 只补 approval ordered evidence，绝不重放 executor 或 terminal。
                    await self._recover_deferred_approval(run.id, state)
                    return RunResult(run_id=run.id, status=persisted_status)
                if terminal_status == RunStatus.COMPLETED.value:
                    output = state.get("output")
                    if not isinstance(output, dict):
                        raise InvalidRunTransition(
                            "delegation terminal checkpoint output is invalid"
                        )
                    terminal = await self._complete(
                        run.id,
                        run.agent_id,
                        output=cast(dict[str, Any], output),
                        identity=execution_identity,
                        request_id=optional_state_text(state, "request_id"),
                        trace_id=run.trace_id,
                        input=run.input,
                        defer_terminal=defer_terminal,
                    )
                    result = RunResult(
                        run_id=run.id,
                        status=RunStatus.COMPLETED,
                        terminal_event=(
                            terminal.event_type.value if terminal is not None else None
                        ),
                    )
                    await self._recover_deferred_approval(run.id, state)
                    return result
                if terminal_status == RunStatus.FAILED.value:
                    error = state.get("error")
                    typed_error = cast(dict[str, Any], error) if isinstance(error, dict) else {}
                    reason = typed_error.get("reason")
                    if not isinstance(reason, str) or not reason:
                        raise InvalidRunTransition(
                            "delegation terminal checkpoint error is invalid"
                        )
                    result = await self.fail_run(
                        run.id,
                        reason=reason,
                        identity=execution_identity,
                        request_id=optional_state_text(state, "request_id"),
                        trace_id=run.trace_id,
                        input=run.input,
                        defer_terminal=defer_terminal,
                    )
                    await self._recover_deferred_approval(run.id, state)
                    return result
                raise InvalidRunTransition("delegation terminal checkpoint status is invalid")
            if RunStatus(run.status) in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run.id}")
            terminal = await self._complete(
                run.id,
                run.agent_id,
                output={"resumed": True},
                identity=active_identity,
                request_id=optional_state_text(state, "request_id"),
                trace_id=run.trace_id,
                input=run.input,
                defer_terminal=defer_terminal,
            )
            return RunResult(
                run_id=run.id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value if terminal is not None else None,
            )
        # Approval-gated token 必须在发布 resumed event、调用 executor 或改变
        # run 前验证 grant；原始 token 永远不能代替 ApprovalGrant。
        if approval_grant is None:
            raise InvalidRunTransition("executor approval resume requires ApprovalGrant")
        # checkpoint 字段只能证明请求语义一致；真正的授权能力来自 repository
        # 中仍有效且未完成的 resolution lease，二者必须在任何 resumed event 或
        # executor/provider 副作用前同时成立。
        await self._validate_persisted_approval_grant(approval_grant)
        if checkpoint_kind == "policy_approval":
            validate_approval_grant(checkpoint.state, approval_grant, active_identity.tenant_id)
            execution_identity = checkpoint_identity(checkpoint.state)
            await self._event_bus.publish(
                tenant_id=execution_identity.tenant_id,
                run_id=run.id,
                agent_id=run.agent_id,
                user_id=execution_identity.user_id,
                event_type=CanonicalEventType.RUN_RESUMED,
                payload={
                    "approval_id": approval_grant.approval_id,
                    **run_correlation(run.input),
                },
                request_id=optional_state_text(checkpoint.state, "request_id"),
                trace_id=run.trace_id,
                event_id=f"run-resumed:{run.id}:{approval_grant.approval_id}",
            )
            terminal = await self._complete(
                run.id,
                run.agent_id,
                output={"resumed": True},
                identity=execution_identity,
                request_id=optional_state_text(checkpoint.state, "request_id"),
                trace_id=run.trace_id,
                input=run.input,
                defer_terminal=defer_terminal,
            )
            return RunResult(
                run_id=run.id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value if terminal is not None else None,
            )
        if self._executor_resolver is None:
            raise InvalidRunTransition("agent executor is not configured")
        validate_approval_grant(checkpoint.state, approval_grant, active_identity.tenant_id)
        execution_identity = checkpoint_identity(checkpoint.state)
        context = build_execution_context(
            identity=execution_identity,
            services=self._executor_services,
            agent_id=run.agent_id,
            run_id=run.id,
            request_id=optional_state_text(checkpoint.state, "request_id"),
            trace_id=run.trace_id,
        )
        request = AgentExecutionRequest(
            agent_id=run.agent_id,
            run_id=run.id,
            input=run.input,
        )

        # tool result 已持久化、run terminal、approval 尚未来得及 finalize 是可恢复
        # 窗口。这里只允许 executor 读取同一 claim 的确定性结果，不重放 handler。
        if RunStatus(run.status) in TERMINAL_STATUSES:
            result = await self._executor_resolver(run.agent_id).resume(
                request,
                context,
                approval_grant,
            )
            validate_terminal_execution_result(RunStatus(run.status), result)
            terminal = None
            if not defer_terminal:
                terminal = await publish_terminal_evidence(
                    self._event_bus,
                    run_id=run.id,
                    agent_id=run.agent_id,
                    status=RunStatus(run.status),
                    identity=execution_identity,
                    output=run.output,
                    error=run.error,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    correlation=run_correlation(run.input),
                )
            return RunResult(
                run_id=run.id,
                status=RunStatus(run.status),
                terminal_event=terminal.event_type.value if terminal is not None else None,
            )

        await self._event_bus.publish(
            tenant_id=execution_identity.tenant_id,
            run_id=run.id,
            agent_id=run.agent_id,
            user_id=execution_identity.user_id,
            event_type=CanonicalEventType.RUN_RESUMED,
            payload={
                "approval_id": approval_grant.approval_id,
                **run_correlation(run.input),
            },
            request_id=context.request_id,
            trace_id=context.trace_id,
            event_id=f"run-resumed:{run.id}:{approval_grant.approval_id}",
        )
        try:
            result = await self._executor_resolver(run.agent_id).resume(
                request,
                context,
                approval_grant,
            )
        except (AgentExecutionLeaseLost, AgentExecutionUncertain):
            raise
        except Exception as exc:  # noqa: BLE001 - deterministic executor failure closes the run
            return await self._recover_delegation_after_wait(
                await self._fail_execution(
                    run.id,
                    run.agent_id,
                    str(redact_secrets(str(exc))),
                    identity=execution_identity,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    input=run.input,
                    defer_terminal=defer_terminal,
                    approval_recovery={
                        "approval_id": approval_grant.approval_id,
                        "actor": active_identity.to_payload(),
                    },
                )
            )
        return await self._apply_execution_result(
            request,
            result,
            context=context,
            defer_terminal=defer_terminal,
            approval_recovery={
                "approval_id": approval_grant.approval_id,
                "actor": active_identity.to_payload(),
            },
        )

    async def _apply_execution_result(
        self,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
        *,
        context: AgentExecutionContext,
        defer_terminal: bool = False,
        approval_recovery: dict[str, Any] | None = None,
    ) -> RunResult:
        """将 executor 的三类结果原子映射为 run 终态或新的审批检查点。

        completed/failed 先检查 delegation terminal 的延后收口窗口；waiting
        只接受完整 approval DTO，并在创建 checkpoint 后委托 ApprovalService
        发布审批记录，避免 executor 直接跨越审批状态机。
        """

        if result.status == "completed":
            deferred = await self._defer_pending_delegation_terminal(
                run_id=request.run_id,
                status=RunStatus.COMPLETED,
                identity=context.identity,
                output=result.output or {},
                request_id=context.request_id,
                trace_id=context.trace_id,
                approval_recovery=approval_recovery,
            )
            if deferred is not None:
                return deferred
            terminal = await self._complete(
                request.run_id,
                request.agent_id,
                output=result.output or {},
                identity=context.identity,
                request_id=context.request_id,
                trace_id=context.trace_id,
                input=request.input,
                defer_terminal=defer_terminal,
            )
            return RunResult(
                run_id=request.run_id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value if terminal is not None else None,
            )
        if result.status == "failed":
            return await self._fail_execution(
                request.run_id,
                request.agent_id,
                result.error or "agent execution failed",
                identity=context.identity,
                request_id=context.request_id,
                trace_id=context.trace_id,
                input=request.input,
                defer_terminal=defer_terminal,
                approval_recovery=approval_recovery,
            )
        approval = result.approval
        if approval is None:  # DTO 已校验，持久化边界仍保留防御
            return await self._fail_execution(
                request.run_id,
                request.agent_id,
                "waiting execution omitted approval request",
                identity=context.identity,
                request_id=context.request_id,
                trace_id=context.trace_id,
                input=request.input,
                defer_terminal=defer_terminal,
                approval_recovery=approval_recovery,
            )
        if self._approval_service is None:
            return await self._fail_execution(
                request.run_id,
                request.agent_id,
                "waiting executor requires an approval service",
                identity=context.identity,
                request_id=context.request_id,
                trace_id=context.trace_id,
                input=request.input,
                defer_terminal=defer_terminal,
            )
        state = approval_checkpoint_state(request, approval, context)
        resume_token = await self._checkpoint(
            request.run_id,
            request.agent_id,
            state,
            identity=context.identity,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )
        await self._approval_service.require_approval(
            actor=context.identity,
            run_id=request.run_id,
            agent_id=request.agent_id,
            action=approval.action,
            resource=approval.resource,
            reason=approval.reason,
            resume_token=resume_token,
            trace_id=context.trace_id,
            request_id=context.request_id,
            metadata={
                "arguments_ref": approval.arguments_ref,
                "arguments_hash": approval.arguments_hash,
                "continuation": approval.continuation,
                "identity_id": context.identity.user_id,
            },
        )
        return RunResult(
            run_id=request.run_id,
            status=RunStatus.WAITING,
            resume_token=resume_token,
        )

    async def _recover_deferred_approval(
        self,
        run_id: str,
        state: dict[str, Any],
    ) -> None:
        """terminal 发布后补完 approval ordered evidence 与公开状态。"""

        recovery = state.get("approval_recovery")
        if recovery is None:
            return
        if not isinstance(recovery, dict):
            raise InvalidRunTransition("delegation approval recovery state is invalid")
        typed_recovery = cast(dict[str, object], recovery)
        approval_id = typed_recovery.get("approval_id")
        actor_payload = typed_recovery.get("actor")
        if not isinstance(approval_id, str) or not isinstance(actor_payload, dict):
            raise InvalidRunTransition("delegation approval recovery state is invalid")
        if self._approval_service is None:
            raise InvalidRunTransition("approval service is not configured")
        actor = IdentityContext.model_validate(cast(dict[str, object], actor_payload))
        await self._approval_service.recover_claimed(
            actor=actor,
            run_id=run_id,
            approval_id=approval_id,
        )
