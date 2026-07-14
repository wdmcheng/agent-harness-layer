"""Checkpoint continuation 与 executor approval 恢复协调。"""

from __future__ import annotations

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

    async def resume_run(
        self,
        resume_token: ResumeToken | str,
        *,
        expected_run_id: str | None = None,
        identity: IdentityContext | None = None,
        approval_grant: ApprovalGrant | None = None,
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
            )
            return RunResult(
                run_id=run.id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value,
            )
        # Approval-gated token 必须在发布 resumed event、调用 executor 或改变
        # run 前验证 grant；原始 token 永远不能代替 ApprovalGrant。
        if approval_grant is None:
            raise InvalidRunTransition("executor approval resume requires ApprovalGrant")
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
            )
            return RunResult(
                run_id=run.id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value,
            )
        if self._executor_resolver is None:
            raise InvalidRunTransition("agent executor is not configured")
        validate_approval_grant(checkpoint.state, approval_grant, active_identity.tenant_id)
        execution_identity = checkpoint_identity(checkpoint.state)
        context = build_execution_context(
            identity=execution_identity,
            services=self._executor_services,
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
                terminal_event=terminal.event_type.value,
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
            return await self._fail_execution(
                run.id,
                run.agent_id,
                str(redact_secrets(str(exc))),
                identity=execution_identity,
                request_id=context.request_id,
                trace_id=context.trace_id,
                input=run.input,
            )
        return await self._apply_execution_result(request, result, context=context)

    async def _apply_execution_result(
        self,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
        *,
        context: AgentExecutionContext,
    ) -> RunResult:
        if result.status == "completed":
            terminal = await self._complete(
                request.run_id,
                request.agent_id,
                output=result.output or {},
                identity=context.identity,
                request_id=context.request_id,
                trace_id=context.trace_id,
                input=request.input,
            )
            return RunResult(
                run_id=request.run_id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value,
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
