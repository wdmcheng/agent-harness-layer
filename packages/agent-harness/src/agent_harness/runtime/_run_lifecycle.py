"""Local run 生命周期、checkpoint 与公开 evidence 协调。"""

from __future__ import annotations

from typing import Any

from agent_harness.events import CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.runtime._orchestrator_support import policy_checkpoint_state, run_correlation
from agent_harness.runtime._run_lifecycle_persistence import RunLifecyclePersistence
from agent_harness.runtime.checkpoints import IdempotencyKey
from agent_harness.runtime.continuation import InvalidRunTransition, idempotency_value
from agent_harness.runtime.evidence import publish_terminal_evidence
from agent_harness.runtime.executor import (
    AgentExecutionRequest,
    RunDetailResult,
    RunResult,
    build_execution_context,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
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


class RunLifecycle(RunLifecyclePersistence):
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
        parent_run_id: str | None = None,
        pre_run_events: list[tuple[CanonicalEventType, dict[str, Any]]] | None = None,
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
                        parent_run_id=parent_run_id,
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
                if parent_run_id is None:
                    budget_runtime = self._executor_services.get("shared_budget")
                    if budget_runtime is not None:
                        if not isinstance(budget_runtime, SharedBudgetRuntime):
                            raise RuntimeError("shared_budget service has an invalid composition")
                        await uow.shared_budget.create_ledger(
                            budget_runtime.ledger_create(
                                tenant_id=tenant.id,
                                run_id=run.id,
                                agent_id=agent_id,
                            )
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
        for event_type, payload in pre_run_events or []:
            await self._event_bus.publish(
                tenant_id=active_identity.tenant_id,
                run_id=run.id,
                agent_id=agent_id,
                user_id=active_identity.user_id,
                event_type=event_type,
                payload={**payload, **run_correlation(run.input)},
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
            agent_id=agent_id,
            run_id=run.id,
            request_id=request_id,
            trace_id=canonical_trace,
        )
        try:
            result = await self._executor_resolver(agent_id).run(request, context)
        except Exception as exc:  # noqa: BLE001 - executor failures become stable run failures
            return await self._recover_delegation_after_wait(
                await self._fail_execution(
                    run.id,
                    agent_id,
                    str(redact_secrets(str(exc))),
                    identity=active_identity,
                    request_id=request_id,
                    trace_id=canonical_trace,
                    input=input,
                )
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
        if existing_status == RunStatus.WAITING:
            return await self._recover_delegation_after_wait(
                RunResult(run_id=existing.id, status=existing_status)
            )
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

    async def get_run_detail(
        self,
        run_id: str,
        *,
        identity: IdentityContext | None = None,
    ) -> RunDetailResult:
        """返回当前 agent 与 parent 关系；delegation 聚合由 application service 补齐。"""

        active_identity = identity or self._identity
        result = await self.get_run(run_id, identity=active_identity)
        async with self._storage.uow() as uow:
            record = await uow.runs.get(run_id)
        if record is None or record.tenant_id != active_identity.tenant_id:
            raise LookupError(f"run not found: {run_id}")
        return RunDetailResult(
            run_id=result.run_id,
            agent_id=record.agent_id,
            status=result.status,
            terminal_event=result.terminal_event,
            parent_run_id=record.parent_run_id,
        )

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
            run = await uow.runs.get_for_update(run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            status = RunStatus(run.status)
            if status in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run_id}")
            await uow.event_capacity.assert_terminal_publishable(run_id=run_id)
            if run.parent_run_id is None:
                await uow.shared_budget.fence_terminal_if_managed(active_identity.tenant_id, run_id)
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
