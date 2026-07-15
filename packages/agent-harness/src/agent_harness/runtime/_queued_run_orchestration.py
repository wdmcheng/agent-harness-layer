"""Service queued run 的持久化、排队与 worker 执行协调。"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from agent_harness.events import CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.runtime._orchestrator_base import OrchestratorState, RunEnqueueUnavailable
from agent_harness.runtime._orchestrator_support import policy_checkpoint_state, run_correlation
from agent_harness.runtime.checkpoints import IdempotencyKey
from agent_harness.runtime.continuation import InvalidRunTransition, idempotency_value
from agent_harness.runtime.evidence import publish_terminal_evidence
from agent_harness.runtime.executor import AgentExecutionRequest, RunResult, build_execution_context
from agent_harness.runtime.queue import RunQueueMessage, build_execute_message
from agent_harness.runtime.state import TERMINAL_STATUSES, RunStatus
from agent_harness.runtime.trace import (
    PreparedRunTrace,
    RunTraceConflict,
    RunTraceIdempotencyConflict,
    normalize_trace_id,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.repositories import (
    RunCreate,
    RunTraceRepositoryConflict,
    SessionCreate,
)


def _validated_execution_identity(
    *,
    execution_context: dict[str, Any],
    run_tenant_id: str,
    private_tenant_id: str,
    operation_tenant_id: str,
) -> IdentityContext:
    """在任何副作用前，把持久化身份快照与权威 tenant 链完整对账。"""

    identity_payload = execution_context.get("identity")
    if not isinstance(identity_payload, dict):
        raise InvalidRunTransition("run execution identity is missing")
    identity = IdentityContext.model_validate(identity_payload)
    if not (identity.tenant_id == private_tenant_id == run_tenant_id == operation_tenant_id):
        raise InvalidRunTransition("run execution tenant mismatch")
    return identity


class QueuedRunOrchestration(OrchestratorState):
    """封装 service API/worker 共享的 durable queue 协调逻辑。"""

    async def submit_run(
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
        """持久化并排队 service run；API 进程不调用 executor。"""

        if self._queue is None:
            raise RunEnqueueUnavailable("run queue is not configured")
        active_identity = identity or self._identity
        first_request_id = request_id or str(uuid4())
        idempotency_key_value = idempotency_value(idempotency_key)
        caller_trace_id = (
            trace_id.caller_trace_id if isinstance(trace_id, PreparedRunTrace) else trace_id
        )
        try:
            async with self._storage.uow() as uow:
                tenant = await uow.tenants.ensure(active_identity.tenant_id)
                session = await uow.sessions.ensure(
                    SessionCreate(
                        session_id=active_identity.session_id,
                        tenant_id=tenant.id,
                        user_id=active_identity.user_id,
                        agent_id=agent_id,
                    )
                )
                existing = None
                if idempotency_key_value is not None:
                    existing = await uow.runs.get_by_idempotency_key(
                        tenant_id=tenant.id,
                        session_id=session.id,
                        agent_id=agent_id,
                        idempotency_key=idempotency_key_value,
                    )
                if existing is not None:
                    if caller_trace_id is not None and existing.trace_id != caller_trace_id:
                        raise RunTraceIdempotencyConflict
                    run = existing
                else:
                    canonical_trace = normalize_trace_id(
                        str(trace_id) if trace_id is not None else None
                    )
                    run = await uow.runs.create_queued(
                        RunCreate(
                            tenant_id=tenant.id,
                            session_id=session.id,
                            agent_id=agent_id,
                            idempotency_key=idempotency_key_value,
                            trace_id=canonical_trace,
                            input=input,
                        ),
                        execution_context={
                            "identity": active_identity.to_payload(),
                            "request_id": first_request_id,
                            "trace_id": canonical_trace,
                            "checkpoint_state": checkpoint_state,
                        },
                        operation_id="run:pending:execute",
                        request_id=first_request_id,
                        effective_idempotency_key=idempotency_key_value,
                        caller_trace_id=caller_trace_id,
                    )
                private = await uow.runs.get_execution(run.id)
                if private is None:
                    raise RuntimeError("idempotent run is not a service queued run")
                await uow.commit()
        except RunTraceRepositoryConflict as exc:
            if exc.code == "trace.idempotency_conflict":
                raise RunTraceIdempotencyConflict from exc
            if exc.code in {"trace.conflict", "trace.idempotency_race"} and (
                idempotency_key_value is not None
            ):
                # 首次查询与 trace claim 之间可能已有同 key 的 queued run 提交。
                # 回读首次记录后再区分同 trace replay 与异 trace 幂等冲突。
                async with self._storage.uow() as uow:
                    run = await uow.runs.get_by_idempotency_key(
                        tenant_id=active_identity.tenant_id,
                        session_id=active_identity.session_id,
                        agent_id=agent_id,
                        idempotency_key=idempotency_key_value,
                    )
                    if run is None:
                        if exc.code == "trace.conflict":
                            raise RunTraceConflict from exc
                        raise
                    if caller_trace_id is not None and run.trace_id != caller_trace_id:
                        raise RunTraceIdempotencyConflict from exc
                    private = await uow.runs.get_execution(run.id)
                    if private is None:
                        raise RuntimeError("idempotent run is not a service queued run") from exc
                    # replay 只使用首次 run 的 durable request/operation evidence；
                    # 后续 enqueue/reconcile 由 operation/event-id 幂等收敛。
            elif exc.code == "trace.conflict":
                raise RunTraceConflict from exc
            else:
                raise

        message = build_execute_message(
            request_id=private.request_id,
            tenant_id=private.tenant_id,
            run_id=private.run_id,
            idempotency_key=private.effective_idempotency_key,
        )
        try:
            queued = await self._queue.enqueue(message)
            await self.reconcile_queued_run(message=message, message_id=queued.message_id)
        except Exception as exc:
            raise RunEnqueueUnavailable("run.enqueue_unavailable") from exc
        return RunResult(run_id=run.id, status=RunStatus.CREATED)

    async def reconcile_queued_run(self, *, message: RunQueueMessage, message_id: str) -> None:
        """worker/API 共同补齐 queued 私有状态与稳定公开 evidence。"""

        if message.kind != "execute_run":
            raise InvalidRunTransition("queue message is not a run execution")
        async with self._storage.uow() as uow:
            run = await uow.runs.get(message.run_id)
            private = await uow.runs.get_execution(message.run_id)
            if (
                run is None
                or private is None
                or run.tenant_id != message.tenant_id
                or private.tenant_id != message.tenant_id
                or private.operation_id != message.operation_id
                or private.request_id != message.request_id
                or private.effective_idempotency_key != message.idempotency_key
            ):
                raise InvalidRunTransition("run queue message does not match storage")
            identity = _validated_execution_identity(
                execution_context=private.execution_context,
                run_tenant_id=run.tenant_id,
                private_tenant_id=private.tenant_id,
                operation_tenant_id=message.tenant_id,
            )
            queued = await uow.runs.mark_queued(
                run_id=message.run_id,
                operation_id=message.operation_id,
                message_id=message_id,
            )
            await uow.commit()
        await self._event_bus.publish(
            tenant_id=identity.tenant_id,
            run_id=run.id,
            agent_id=run.agent_id,
            user_id=identity.user_id,
            event_type=CanonicalEventType.RUN_QUEUED,
            payload={"agent_id": run.agent_id, **run_correlation(run.input)},
            visibility="public",
            request_id=queued.request_id,
            trace_id=run.trace_id,
            event_id=f"run-queued:{run.id}",
        )

    async def execute_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        operation_id: str,
        owner_id: str,
        workflow_id: str,
    ) -> RunResult:
        """worker 从持久化 context 执行已存在 run，不创建第二条记录。"""

        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            private = await uow.runs.get_execution(run_id)
            if (
                run is None
                or private is None
                or run.tenant_id != tenant_id
                or private.tenant_id != tenant_id
                or private.operation_id != operation_id
            ):
                raise LookupError(f"run not found: {run_id}")
            execution_identity = _validated_execution_identity(
                execution_context=private.execution_context,
                run_tenant_id=run.tenant_id,
                private_tenant_id=private.tenant_id,
                operation_tenant_id=tenant_id,
            )
            status = RunStatus(run.status)
            if status in TERMINAL_STATUSES or status == RunStatus.WAITING:
                return RunResult(run_id=run_id, status=status)
            claimed = await uow.runs.claim_execution(
                run_id=run_id,
                operation_id=operation_id,
                owner_id=owner_id,
                workflow_id=workflow_id,
            )
            if not claimed:
                raise InvalidRunTransition("run execution owner mismatch")
            await uow.commit()

        await self.recover_pending_usage_evidence(run_id=run_id)
        context_payload = private.execution_context
        request_id = context_payload.get("request_id")
        request_id_value = request_id if isinstance(request_id, str) else None
        trace_id_value = run.trace_id
        await self._event_bus.publish(
            tenant_id=execution_identity.tenant_id,
            run_id=run_id,
            agent_id=run.agent_id,
            user_id=execution_identity.user_id,
            event_type=CanonicalEventType.RUN_STARTED,
            payload={"agent_id": run.agent_id, **run_correlation(run.input)},
            visibility="public",
            request_id=request_id_value,
            trace_id=trace_id_value,
            event_id=f"run-started:{run_id}",
        )
        checkpoint_state = context_payload.get("checkpoint_state")
        if isinstance(checkpoint_state, dict):
            state = policy_checkpoint_state(
                run_id=run_id,
                agent_id=run.agent_id,
                checkpoint_state=cast(dict[str, Any], checkpoint_state),
                identity=execution_identity,
                request_id=request_id_value,
                trace_id=trace_id_value,
            )
            resume_token = await self._checkpoint(
                run_id,
                run.agent_id,
                state,
                identity=execution_identity,
                request_id=request_id_value,
                trace_id=trace_id_value,
            )
            if state.get("kind") == "policy_approval":
                if self._approval_service is None:
                    return await self._fail_execution(
                        run_id,
                        run.agent_id,
                        "policy checkpoint requires an approval service",
                        identity=execution_identity,
                        request_id=request_id_value,
                        trace_id=trace_id_value,
                        input=run.input,
                    )
                await self._approval_service.require_approval(
                    actor=execution_identity,
                    run_id=run_id,
                    agent_id=run.agent_id,
                    action=str(state["action"]),
                    resource=str(state["resource"]),
                    reason=str(state["reason"]),
                    resume_token=resume_token,
                    trace_id=trace_id_value,
                    request_id=request_id_value,
                    metadata={
                        "identity_id": state["identity_id"],
                        "arguments_hash": state["arguments_hash"],
                    },
                )
            return RunResult(run_id=run_id, status=RunStatus.WAITING, resume_token=resume_token)
        if self._executor_resolver is None:
            return await self._fail_execution(
                run_id,
                run.agent_id,
                "agent executor is not configured",
                identity=execution_identity,
                request_id=request_id_value,
                trace_id=trace_id_value,
                input=run.input,
            )
        request = AgentExecutionRequest(agent_id=run.agent_id, run_id=run.id, input=run.input)
        context = build_execution_context(
            identity=execution_identity,
            services=self._executor_services,
            agent_id=run.agent_id,
            run_id=run.id,
            request_id=request_id_value,
            trace_id=trace_id_value,
        )
        try:
            result = await self._executor_resolver(run.agent_id).run(request, context)
        except Exception as exc:  # noqa: BLE001 - deterministic executor failure closes the run
            return await self._fail_execution(
                run_id,
                run.agent_id,
                str(redact_secrets(str(exc))),
                identity=execution_identity,
                request_id=request_id_value,
                trace_id=trace_id_value,
                input=run.input,
            )
        return await self._apply_execution_result(request, result, context=context)

    async def fail_queued_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        reason: str,
        defer_terminal: bool = False,
    ) -> RunResult:
        """用持久化身份把 DBOS 确定性失败收口为 application terminal。"""

        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            private = await uow.runs.get_execution(run_id)
        if (
            run is None
            or private is None
            or run.tenant_id != tenant_id
            or private.tenant_id != tenant_id
        ):
            raise LookupError(f"run not found: {run_id}")
        identity = _validated_execution_identity(
            execution_context=private.execution_context,
            run_tenant_id=run.tenant_id,
            private_tenant_id=private.tenant_id,
            operation_tenant_id=tenant_id,
        )
        status = RunStatus(run.status)
        request_id = private.execution_context.get("request_id")
        trace_id = private.execution_context.get("trace_id")
        request_id_value = request_id if isinstance(request_id, str) else None
        trace_id_value = trace_id if isinstance(trace_id, str) else None
        if status in TERMINAL_STATUSES:
            terminal = None
            if not defer_terminal:
                terminal = await publish_terminal_evidence(
                    self._event_bus,
                    run_id=run_id,
                    agent_id=run.agent_id,
                    status=status,
                    identity=identity,
                    output=run.output,
                    error=run.error,
                    request_id=request_id_value,
                    trace_id=trace_id_value,
                    correlation=run_correlation(run.input),
                )
            return RunResult(
                run_id=run_id,
                status=status,
                terminal_event=terminal.event_type.value if terminal is not None else None,
            )
        return await self.fail_run(
            run_id,
            reason=reason,
            identity=identity,
            request_id=request_id_value,
            trace_id=trace_id_value,
            input=run.input,
            defer_terminal=defer_terminal,
        )
