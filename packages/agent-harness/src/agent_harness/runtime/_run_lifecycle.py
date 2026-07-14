"""Local run 生命周期、checkpoint 与公开 evidence 协调。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from agent_harness.events import CanonicalEvent, CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.runtime._orchestrator_base import OrchestratorState
from agent_harness.runtime._orchestrator_support import policy_checkpoint_state, run_correlation
from agent_harness.runtime.checkpoints import IdempotencyKey, ResumeToken
from agent_harness.runtime.continuation import InvalidRunTransition, idempotency_value
from agent_harness.runtime.evidence import persist_failed_execution, publish_terminal_evidence
from agent_harness.runtime.executor import AgentExecutionRequest, RunResult, build_execution_context
from agent_harness.runtime.state import TERMINAL_STATUSES, RunStatus
from agent_harness.runtime.trace import (
    PreparedRunTrace,
    RunTraceConflict,
    RunTraceIdempotencyConflict,
    normalize_trace_id,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.repositories import (
    CheckpointCreate,
    RunCreate,
    RunTraceRepositoryConflict,
    SessionCreate,
)


class RunLifecycle(OrchestratorState):
    """封装 provider-neutral 的创建、查询、终止与 evidence 生命周期。"""

    async def prepare_trace(
        self,
        *,
        agent_id: str,
        idempotency_key: IdempotencyKey | str | None = None,
        identity: IdentityContext | None = None,
        trace_id: str | PreparedRunTrace | None = None,
    ) -> PreparedRunTrace:
        """在 policy/provider 等副作用前只读解析 canonical trace 与冲突。

        首次预检返回的 ``PreparedRunTrace`` 可在全局 trace 锁内复用同一候选值；
        其 ``caller_trace_id`` 仍保留调用方是否显式指定 trace 的幂等语义。
        """

        active_identity = identity or self._identity
        idempotency_key_value = idempotency_value(idempotency_key)
        caller_trace_id = (
            trace_id.caller_trace_id if isinstance(trace_id, PreparedRunTrace) else trace_id
        )
        candidate = normalize_trace_id(
            str(trace_id) if isinstance(trace_id, PreparedRunTrace) else trace_id
        )
        async with self._storage.uow() as uow:
            existing = None
            if idempotency_key_value is not None:
                existing = await uow.runs.get_by_idempotency_key(
                    tenant_id=active_identity.tenant_id,
                    session_id=active_identity.session_id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_key_value,
                )
            if existing is not None:
                if caller_trace_id is not None and candidate != existing.trace_id:
                    raise RunTraceIdempotencyConflict
                return PreparedRunTrace(
                    existing.trace_id,
                    caller_trace_id=caller_trace_id,
                    replays_existing=True,
                )
            if (
                await uow.runs.get_trace_binding_root(
                    tenant_id=active_identity.tenant_id,
                    trace_id=candidate,
                )
                is not None
            ):
                raise RunTraceConflict
            if await uow.runs.trace_binding_exists(trace_id=candidate):
                raise RunTraceConflict
        return PreparedRunTrace(
            candidate,
            caller_trace_id=caller_trace_id,
            replays_existing=False,
        )

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
        """创建 local run，执行 executor，必要时停在 checkpoint。"""

        active_identity = identity or self._identity
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
                    return await self._replay_existing_run(
                        existing=existing,
                        caller_trace_id=caller_trace_id,
                        identity=active_identity,
                    )

                canonical_trace = normalize_trace_id(
                    str(trace_id) if trace_id is not None else None
                )
                run = await uow.runs.create(
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
                        "request_id": request_id,
                        "trace_id": canonical_trace,
                        "checkpoint_state": checkpoint_state,
                    },
                    caller_trace_id=caller_trace_id,
                )
                await uow.runs.set_status(run.id, RunStatus.RUNNING.value)
                await uow.commit()
        except RunTraceRepositoryConflict as exc:
            if exc.code == "trace.idempotency_conflict":
                raise RunTraceIdempotencyConflict from exc
            if exc.code in {"trace.conflict", "trace.idempotency_race"} and (
                idempotency_key_value is not None
            ):
                # 首次幂等查询与 trace claim 之间可能有同 key 的事务刚提交。
                # 重新读取首次 run 后由统一 replay 逻辑区分同 trace 与异 trace。
                async with self._storage.uow() as uow:
                    existing = await uow.runs.get_by_idempotency_key(
                        tenant_id=active_identity.tenant_id,
                        session_id=active_identity.session_id,
                        agent_id=agent_id,
                        idempotency_key=idempotency_key_value,
                    )
                if existing is not None:
                    return await self._replay_existing_run(
                        existing=existing,
                        caller_trace_id=caller_trace_id,
                        identity=active_identity,
                    )
            if exc.code == "trace.conflict":
                raise RunTraceConflict from exc
            raise

        await self._event_bus.publish(
            tenant_id=active_identity.tenant_id,
            run_id=run.id,
            agent_id=agent_id,
            user_id=active_identity.user_id,
            event_type=CanonicalEventType.RUN_STARTED,
            payload={"agent_id": agent_id, **run_correlation(run.input)},
            visibility="public",
            request_id=request_id,
            trace_id=canonical_trace,
        )
        if checkpoint_state is not None:
            state = policy_checkpoint_state(
                run_id=run.id,
                agent_id=agent_id,
                checkpoint_state=checkpoint_state,
                identity=active_identity,
                request_id=request_id,
                trace_id=canonical_trace,
            )
            resume_token = await self._checkpoint(
                run.id,
                agent_id,
                state,
                identity=active_identity,
                request_id=request_id,
                trace_id=canonical_trace,
            )
            return RunResult(run_id=run.id, status=RunStatus.WAITING, resume_token=resume_token)
        if self._executor_resolver is None:
            return await self._fail_execution(
                run.id,
                agent_id,
                "agent executor is not configured",
                identity=active_identity,
                request_id=request_id,
                trace_id=canonical_trace,
                input=input,
            )
        request = AgentExecutionRequest(agent_id=agent_id, run_id=run.id, input=input)
        context = build_execution_context(
            identity=active_identity,
            services=self._executor_services,
            request_id=request_id,
            trace_id=canonical_trace,
        )
        try:
            result = await self._executor_resolver(agent_id).run(request, context)
        except Exception as exc:  # noqa: BLE001 - executor failures become stable run failures
            return await self._fail_execution(
                run.id,
                agent_id,
                str(redact_secrets(str(exc))),
                identity=active_identity,
                request_id=request_id,
                trace_id=canonical_trace,
                input=input,
            )
        return await self._apply_execution_result(request, result, context=context)

    async def _replay_existing_run(
        self,
        *,
        existing: Any,
        caller_trace_id: str | None,
        identity: IdentityContext,
    ) -> RunResult:
        """并发/顺序重放只读取首次 run；caller 缺失不与内部候选比较。"""

        if caller_trace_id is not None and normalize_trace_id(caller_trace_id) != existing.trace_id:
            raise RunTraceIdempotencyConflict
        existing_status = RunStatus(existing.status)
        if (
            existing_status in TERMINAL_STATUSES
            and await self._event_bus.terminal_event(existing.id) is None
        ):
            await publish_terminal_evidence(
                self._event_bus,
                run_id=existing.id,
                agent_id=existing.agent_id,
                status=existing_status,
                identity=identity,
                output=existing.output,
                error=existing.error,
                trace_id=existing.trace_id,
            )
        return RunResult(run_id=existing.id, status=existing_status)

    async def get_run(
        self,
        run_id: str,
        *,
        identity: IdentityContext | None = None,
    ) -> RunResult:
        """读取 run lifecycle 摘要，不向 API/CLI 泄漏 ORM。"""

        active_identity = identity or self._identity
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            status = RunStatus(run.status)
            private = await uow.runs.get_execution(run_id)
        terminal_event = None
        if status in TERMINAL_STATUSES:
            terminal = await self._event_bus.terminal_event(run_id)
            if terminal is None:
                private_request_id = (
                    private.execution_context.get("request_id") if private is not None else None
                )
                terminal = await publish_terminal_evidence(
                    self._event_bus,
                    run_id=run_id,
                    agent_id=run.agent_id,
                    status=status,
                    identity=active_identity,
                    output=run.output,
                    error=run.error,
                    request_id=(
                        private_request_id if isinstance(private_request_id, str) else None
                    ),
                    trace_id=run.trace_id,
                    correlation=run_correlation(run.input),
                )
            terminal_event = terminal.event_type.value
        return RunResult(run_id=run_id, status=status, terminal_event=terminal_event)

    async def cancel_run(
        self,
        run_id: str,
        *,
        identity: IdentityContext | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
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
            request_id=request_id,
            trace_id=run.trace_id,
            correlation=run_correlation(run.input),
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
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> RunResult:
        """把非 terminal run 转为 failed，并公开脱敏失败 evidence。"""

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
            request_id=request_id,
            trace_id=run.trace_id,
            correlation=run_correlation(input or run.input),
        )
        return RunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_event=terminal.event_type.value,
        )

    async def _fail_execution(
        self,
        run_id: str,
        agent_id: str,
        reason: str,
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> RunResult:
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
        if run is None or run.tenant_id != identity.tenant_id:
            raise LookupError(f"run not found: {run_id}")
        return await persist_failed_execution(
            self._storage,
            self._event_bus,
            run_id=run_id,
            agent_id=agent_id,
            reason=reason,
            identity=identity,
            request_id=request_id,
            trace_id=run.trace_id,
            correlation=run_correlation(input or {}),
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
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            state = {**state, "trace_id": run.trace_id}
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
            payload={"state": state, **run_correlation(run.input)},
            request_id=request_id,
            trace_id=run.trace_id,
        )
        return resume_token

    async def record_guardrail_check(
        self,
        *,
        run_id: str,
        agent_id: str,
        identity: IdentityContext,
        payload: dict[str, Any],
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> CanonicalEvent:
        """把 run 创建前的输入 guardrail 决策挂回 run event stream。"""

        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
        if run is None or run.tenant_id != identity.tenant_id:
            raise LookupError(f"run not found: {run_id}")
        return await self._event_bus.publish(
            tenant_id=identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=identity.user_id,
            event_type=CanonicalEventType.INPUT_GUARDRAIL_CHECKED,
            payload={**payload, **run_correlation(run.input)},
            request_id=request_id,
            trace_id=run.trace_id,
        )

    async def _complete(
        self,
        run_id: str,
        agent_id: str,
        output: dict[str, Any],
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> CanonicalEvent:
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            await uow.runs.set_status(run_id, RunStatus.COMPLETED.value, output=output)
            await uow.commit()
        return await publish_terminal_evidence(
            self._event_bus,
            run_id=run_id,
            agent_id=agent_id,
            status=RunStatus.COMPLETED,
            identity=identity,
            output=output,
            request_id=request_id,
            trace_id=run.trace_id,
            correlation=run_correlation(input or {}),
        )
