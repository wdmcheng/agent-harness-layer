"""最小可运行 runtime orchestrator，负责 run、checkpoint 和事件边界。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.checkpoints import IdempotencyKey, ResumeToken
from agent_harness.runtime.continuation import (
    InvalidRunTransition,
    approval_checkpoint_state,
    checkpoint_identity,
    idempotency_value,
    optional_state_text,
    resume_token_value,
    validate_approval_grant,
    validate_terminal_execution_result,
)
from agent_harness.runtime.evidence import persist_failed_execution, publish_terminal_evidence
from agent_harness.runtime.executor import (
    AgentExecutionContext,
    AgentExecutionLeaseLost,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionUncertain,
    AgentExecutorResolver,
    ApprovalGrant,
    RunResult,
    build_execution_context,
)
from agent_harness.runtime.state import TERMINAL_STATUSES, RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.repositories import CheckpointCreate, RunCreate, SessionCreate


class RunOrchestrator:
    """协调持久化 run 记录、checkpoint 和 CanonicalEvent 输出。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        identity: IdentityContext | None = None,
        executor_resolver: AgentExecutorResolver | None = None,
        executor_services: Mapping[str, object] | None = None,
    ) -> None:
        self._storage = storage
        self._event_bus = event_bus
        self._identity = identity or IdentityContext.local_default()
        self._executor_resolver = executor_resolver
        self._executor_services = dict(executor_services or {})
        self._approval_service: Any | None = None

    def bind_approval_service(self, service: Any) -> None:
        """闭合 runtime/approval 调用环，但不持久化 service object。"""

        self._approval_service = service

    async def start_run(
        self,
        *,
        agent_id: str,
        input: dict[str, Any],
        idempotency_key: IdempotencyKey | str | None = None,
        checkpoint_state: dict[str, Any] | None = None,
        identity: IdentityContext | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> RunResult:
        """创建 run 并写入公开事件，必要时停在 checkpoint 等待外部恢复。

        幂等键在 tenant/session/agent 维度内生效；调用方拿到的始终是 DTO，
        不会泄漏 repository 或 ORM model。
        """

        active_identity = identity or self._identity
        idempotency_key_value = idempotency_value(idempotency_key)
        async with self._storage.uow() as uow:
            # identity 归属记录统一通过 repository 创建，API、CLI 和 worker
            # 路径都会穿过同一个 UoW 边界。
            tenant = await uow.tenants.ensure(active_identity.tenant_id)
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=active_identity.session_id,
                    tenant_id=tenant.id,
                    user_id=active_identity.user_id,
                    agent_id=agent_id,
                )
            )
            # idempotency 在持久化存储里解析，不放进内存；service 重启后仍能
            # 安全响应重复提交。
            existing = None
            if idempotency_key_value is not None:
                existing = await uow.runs.get_by_idempotency_key(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_key_value,
                )
            if existing is not None:
                existing_status = RunStatus(existing.status)
                if existing_status in TERMINAL_STATUSES:
                    await publish_terminal_evidence(
                        self._event_bus,
                        run_id=existing.id,
                        agent_id=existing.agent_id,
                        status=existing_status,
                        identity=active_identity,
                        output=existing.output,
                        error=existing.error,
                    )
                return RunResult(run_id=existing.id, status=existing_status)

            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_key_value,
                    input=input,
                )
            )
            await uow.runs.set_status(run.id, RunStatus.RUNNING.value)
            await uow.commit()

        await self._event_bus.publish(
            tenant_id=active_identity.tenant_id,
            run_id=run.id,
            agent_id=agent_id,
            user_id=active_identity.user_id,
            event_type=CanonicalEventType.RUN_STARTED,
            payload={"agent_id": agent_id},
            visibility="public",
            request_id=request_id,
            trace_id=trace_id,
        )
        # 显式 checkpoint_state 是 guardrail 使用的底层暂停 seam，不得伪造
        # executor success result。
        if checkpoint_state is not None:
            resume_token = await self._checkpoint(
                run.id,
                agent_id,
                checkpoint_state,
                identity=active_identity,
                request_id=request_id,
                trace_id=trace_id,
            )
            return RunResult(run_id=run.id, status=RunStatus.WAITING, resume_token=resume_token)
        if self._executor_resolver is None:
            return await self._fail_execution(
                run.id,
                agent_id,
                "agent executor is not configured",
                identity=active_identity,
            )
        request = AgentExecutionRequest(agent_id=agent_id, run_id=run.id, input=input)
        context = build_execution_context(
            identity=active_identity,
            services=self._executor_services,
            request_id=request_id,
            trace_id=trace_id,
        )
        try:
            result = await self._executor_resolver(agent_id).run(request, context)
        except Exception as exc:  # noqa: BLE001 - executor failures must become stable run failures
            return await self._fail_execution(
                run.id,
                agent_id,
                str(redact_secrets(str(exc))),
                identity=active_identity,
            )
        return await self._apply_execution_result(
            request,
            result,
            context=context,
        )

    async def get_run(
        self,
        run_id: str,
        *,
        identity: IdentityContext | None = None,
    ) -> RunResult:
        """读取 run lifecycle 摘要，供 API/CLI 不碰 ORM 的 detail route 使用。"""

        active_identity = identity or self._identity
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            status = RunStatus(run.status)
        terminal_event = None
        if status in TERMINAL_STATUSES:
            terminal = await publish_terminal_evidence(
                self._event_bus,
                run_id=run_id,
                agent_id=run.agent_id,
                status=status,
                identity=active_identity,
                output=run.output,
                error=run.error,
            )
            terminal_event = terminal.event_type.value
        return RunResult(run_id=run_id, status=status, terminal_event=terminal_event)

    async def cancel_run(
        self,
        run_id: str,
        *,
        identity: IdentityContext | None = None,
    ) -> RunResult:
        """把非 terminal run 转为 cancelled，并发布唯一 terminal event。"""

        active_identity = identity or self._identity
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            status = RunStatus(run.status)
            if status in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run_id}")
            await uow.runs.set_status(run_id, RunStatus.CANCELLED.value)
            await uow.commit()
        terminal = await publish_terminal_evidence(
            self._event_bus,
            run_id=run_id,
            agent_id=run.agent_id,
            status=RunStatus.CANCELLED,
            identity=active_identity,
        )
        return RunResult(
            run_id=run_id,
            status=RunStatus.CANCELLED,
            terminal_event=terminal.event_type.value,
        )

    async def fail_run(
        self,
        run_id: str,
        *,
        reason: str,
        identity: IdentityContext | None = None,
    ) -> RunResult:
        """把非 terminal run 转为 failed，并把失败原因写入公开 terminal event。"""

        active_identity = identity or self._identity
        safe_reason = str(redact_secrets(reason))
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            if RunStatus(run.status) in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run.id}")
            await uow.runs.set_status(
                run_id,
                RunStatus.FAILED.value,
                error={"reason": safe_reason},
            )
            await uow.commit()
        terminal = await publish_terminal_evidence(
            self._event_bus,
            run_id=run_id,
            agent_id=run.agent_id,
            status=RunStatus.FAILED,
            identity=active_identity,
            error={"reason": safe_reason},
        )
        return RunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_event=terminal.event_type.value,
        )

    async def resume_run(
        self,
        resume_token: ResumeToken | str,
        *,
        expected_run_id: str | None = None,
        identity: IdentityContext | None = None,
        approval_grant: ApprovalGrant | None = None,
    ) -> RunResult:
        """用 resume token 完成等待中的 run。

        如果 API URL 带有 run_id，必须通过 `expected_run_id` 校验 token 归属，
        防止错误路径推进另一个 run。
        """

        active_identity = identity or self._identity
        token_value = resume_token_value(resume_token)
        async with self._storage.uow() as uow:
            checkpoint = await uow.checkpoints.get_by_resume_token(token_value)
            if checkpoint is None or checkpoint.tenant_id != active_identity.tenant_id:
                raise LookupError("checkpoint not found")
            # API route 的 path 里有 run_id。必须先校验 token 归属再完成 run，
            # 否则错误 URL 会推进 token 所属的另一个 run。
            if expected_run_id is not None and checkpoint.run_id != expected_run_id:
                raise LookupError("resume token does not belong to run")
            run = await uow.runs.get(checkpoint.run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {checkpoint.run_id}")
        state = checkpoint.state
        is_approval_checkpoint = state.get("kind") == "agent_executor_approval"
        if not is_approval_checkpoint:
            if RunStatus(run.status) in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run.id}")
            terminal = await self._complete(
                run.id,
                run.agent_id,
                output={"resumed": True},
                identity=active_identity,
            )
            return RunResult(
                run_id=run.id,
                status=RunStatus.COMPLETED,
                terminal_event=terminal.event_type.value,
            )
        # Approval-gated token 的公开请求必须在发布 resumed event、调用 executor
        # 或改变 run 前失败；原始 token 永远不能代替 ApprovalGrant。
        if approval_grant is None:
            raise InvalidRunTransition("executor approval resume requires ApprovalGrant")
        if self._executor_resolver is None:
            raise InvalidRunTransition("agent executor is not configured")
        validate_approval_grant(checkpoint.state, approval_grant, active_identity.tenant_id)
        execution_identity = checkpoint_identity(checkpoint.state)
        context = build_execution_context(
            identity=execution_identity,
            services=self._executor_services,
            request_id=optional_state_text(checkpoint.state, "request_id"),
            trace_id=optional_state_text(checkpoint.state, "trace_id"),
        )
        request = AgentExecutionRequest(
            agent_id=run.agent_id,
            run_id=run.id,
            input=run.input,
        )

        # tool result 已持久化、run terminal、approval 尚未来得及 finalize 是可恢复
        # 窗口。此时只允许 executor 读取同一 claim 的确定性结果，不发布第二个
        # terminal event；ToolRegistry 的 unique approval_id 保证 handler 不重放。
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
            payload={"approval_id": approval_grant.approval_id},
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
            )
        approval = result.approval
        if approval is None:  # DTO 已校验，持久化边界仍保留防御
            return await self._fail_execution(
                request.run_id,
                request.agent_id,
                "waiting execution omitted approval request",
                identity=context.identity,
            )
        if self._approval_service is None:
            return await self._fail_execution(
                request.run_id,
                request.agent_id,
                "waiting executor requires an approval service",
                identity=context.identity,
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

    async def _fail_execution(
        self,
        run_id: str,
        agent_id: str,
        reason: str,
        *,
        identity: IdentityContext,
    ) -> RunResult:
        return await persist_failed_execution(
            self._storage,
            self._event_bus,
            run_id=run_id,
            agent_id=agent_id,
            reason=reason,
            identity=identity,
        )

    async def _checkpoint(
        self,
        run_id: str,
        agent_id: str,
        state: dict[str, Any],
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> ResumeToken:
        resume_token = ResumeToken(value=f"resume-{uuid4()}")
        async with self._storage.uow() as uow:
            await uow.checkpoints.create(
                CheckpointCreate(
                    tenant_id=identity.tenant_id,
                    run_id=run_id,
                    sequence=1,
                    resume_token=resume_token.value,
                    state=state,
                )
            )
            await uow.runs.set_status(run_id, RunStatus.WAITING.value)
            await uow.commit()
        await self._event_bus.publish(
            tenant_id=identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=identity.user_id,
            event_type=CanonicalEventType.CHECKPOINT_CREATED,
            payload={"state": state},
            request_id=request_id,
            trace_id=trace_id,
        )
        return resume_token

    async def record_guardrail_check(
        self,
        *,
        run_id: str,
        agent_id: str,
        identity: IdentityContext,
        payload: dict[str, Any],
    ) -> CanonicalEvent:
        """把 run 创建前的输入 guardrail 决策挂回 run event stream。"""

        return await self._event_bus.publish(
            tenant_id=identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=identity.user_id,
            event_type=CanonicalEventType.INPUT_GUARDRAIL_CHECKED,
            payload=payload,
        )

    async def _complete(
        self,
        run_id: str,
        agent_id: str,
        output: dict[str, Any],
        *,
        identity: IdentityContext,
    ) -> CanonicalEvent:
        async with self._storage.uow() as uow:
            await uow.runs.set_status(run_id, RunStatus.COMPLETED.value, output=output)
            await uow.commit()
        return await publish_terminal_evidence(
            self._event_bus,
            run_id=run_id,
            agent_id=agent_id,
            status=RunStatus.COMPLETED,
            identity=identity,
            output=output,
        )
