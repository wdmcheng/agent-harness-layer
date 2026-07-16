"""真实 DelegationService 的授权、重放与 local child 状态机合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import pytest
from sqlalchemy import select, update

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.delegation import (
    DelegationRequest,
    delegation_request_hash,
)
from agent_harness.delegation.service import DelegationError, DelegationMode, DelegationService
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    EventSink,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.events.serialization import canonical_event_bytes
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelDecision,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import RunDetailResult, RunResult, RunStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.delegation_repositories import DelegationClaimCreate
from agent_harness.storage.event_capacity_repositories import (
    MAX_EVENT_SEQ,
    EvidenceOperationKind,
)
from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository
from agent_harness.storage.models import AgentRunModel, RunEvidenceOutboxModel, SessionModel
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver
from app.api.routes.runs import get_run_with_orchestrator


def _descriptor(
    agent_id: str,
    *,
    targets: list[str],
    max_tokens: int = 100,
    max_cost_usd: float | None = 10.0,
) -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=agent_id,
        version="1",
        name=agent_id,
        description="delegation contract agent",
        input_schema_ref="schemas/input.json",
        output_schema_ref="schemas/output.json",
        config_ref=f"agents/{agent_id}/config.yaml",
        tool_policy=AgentToolPolicy(allowed_tools=["agent.delegate"]),
        model_policy=AgentModelPolicy(
            provider="fake",
            default_model="fake-basic",
            fallback_models=[],
        ),
        budget=AgentBudget(
            max_tokens_per_run=max_tokens,
            max_cost_usd_per_run=max_cost_usd,
        ),
        eval_dataset=None,
        delegation_targets=targets,
    )


def _identity(*, permissions: list[str] | None = None) -> IdentityContext:
    return IdentityContext(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        roles=["operator"],
        permissions=permissions if permissions is not None else ["agent.delegate"],
        auth_method="api-key",
    )


class _Policy:
    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        decision = (
            GuardrailDecisionStatus.ALLOW.value
            if "agent.delegate" in check.actor.permissions
            else GuardrailDecisionStatus.DENY.value
        )
        return PolicyEvaluation(
            decision=decision,
            reason="contract policy",
            actor=check.actor,
            action=check.action,
            resource=check.resource,
        )


class _ParentDetailOrchestrator:
    async def get_run_detail(self, run_id: str, **_: object) -> RunDetailResult:
        return RunDetailResult(
            run_id=run_id,
            agent_id="agent-source",
            status=RunStatus.RUNNING,
            terminal_event=None,
            parent_run_id=None,
        )


class _InlineChildRuntime:
    """只模拟 runtime 持久化 child；usage 缺失应由 service 保持 needs_review。"""

    def __init__(
        self,
        storage: SQLAlchemyStorage,
        *,
        usage_service: ModelInvocationService | None = None,
        launch_error: bool = False,
        child_status: RunStatus = RunStatus.COMPLETED,
    ) -> None:
        self.storage = storage
        self.usage_service = usage_service
        self.launch_error = launch_error
        self.child_status = child_status
        self.calls = 0

    async def start_run(self, **kwargs: Any) -> RunResult:
        self.calls += 1
        if self.launch_error:
            raise RuntimeError("deterministic child creation failure")
        identity = kwargs["identity"]
        async with self.storage.uow() as uow:
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=identity.session_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_id=kwargs["agent_id"],
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=identity.tenant_id,
                    session_id=session.id,
                    agent_id=kwargs["agent_id"],
                    idempotency_key=str(kwargs["idempotency_key"]),
                    parent_run_id=kwargs["parent_run_id"],
                    trace_id=kwargs["trace_id"],
                    input=kwargs["input"],
                )
            )
            await uow.commit()
        if self.usage_service is not None:
            await self.usage_service.complete(
                ModelRequest(provider="fake", prompt="child usage", max_output_tokens=2),
                context=UsageEvidenceContext(
                    tenant_id=identity.tenant_id,
                    run_id=run.id,
                    agent_id=kwargs["agent_id"],
                    request_id=kwargs["request_id"],
                    trace_id=kwargs["trace_id"],
                ),
                usage_call_id=f"delegation-child:{run.id}:model",
            )
            async with self.storage.uow() as uow:
                usage_rows = await uow.delegations.usage_evidence_for_child(run.id)
            assert len(usage_rows) == 1
        async with self.storage.uow() as uow:
            await uow.runs.set_status(
                run.id,
                self.child_status.value,
                output={"ok": True} if self.child_status == RunStatus.COMPLETED else None,
                error=(
                    {
                        "code": "child.failed",
                        "message": "redacted deterministic child failure",
                    }
                    if self.child_status == RunStatus.FAILED
                    else None
                ),
            )
            await uow.commit()
        return RunResult(run_id=run.id, status=self.child_status)

    async def resume_run(self, resume_token: str, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected parent resume: {resume_token}")

    async def submit_run(self, **kwargs: Any) -> RunResult:
        return await self.start_run(**kwargs)


async def _build_service(
    tmp_path: Path,
    *,
    database_events: bool = False,
    source_targets: list[str] | None = None,
    include_target: bool = True,
    delegated_parent: bool = False,
    trustworthy_usage: bool = False,
    mode: DelegationMode = "local",
    launch_error: bool = False,
    child_status: RunStatus = RunStatus.COMPLETED,
    source_cost_limit: float | None = 10.0,
    target_token_limit: int = 100,
    target_cost_limit: float | None = 10.0,
    usage_cost_usd: float = 0.25,
) -> tuple[
    SQLAlchemyStorage,
    DelegationService,
    _InlineChildRuntime,
    str,
    EventSink,
]:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'delegation-service.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-source",
            )
        )
        root = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-source",
                trace_id="trace-parent",
            )
        )
        parent = root
        if delegated_parent:
            parent = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id=session.id,
                    agent_id="agent-source",
                    parent_run_id=root.id,
                    trace_id=root.trace_id,
                )
            )
        await uow.commit()
    resolver = StorageRunTraceResolver(storage)
    sink: EventSink = (
        PostgreSQLEventSink(storage)
        if database_events
        else LocalJsonlEventSink(
            tmp_path / "events.jsonl",
            state_dir=tmp_path,
            run_trace_resolver=resolver,
        )
    )
    event_bus = EventBus(
        sink=sink,
        run_trace_resolver=resolver,
        capacity_storage=None if database_events else storage,
    )
    usage_service = None
    if trustworthy_usage:
        usage_service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": _UsageProvider(cost_usd=usage_cost_usd)},
            ),
            storage=storage,
            event_bus=event_bus,
        )
    runtime = _InlineChildRuntime(
        storage,
        usage_service=usage_service,
        launch_error=launch_error,
        child_status=child_status,
    )
    service = DelegationService(
        storage=storage,
        registry=AgentRegistry(
            [
                _descriptor(
                    "agent-source",
                    targets=(source_targets if source_targets is not None else ["agent-target"]),
                    max_cost_usd=source_cost_limit,
                )
            ]
            + (
                [
                    _descriptor(
                        "agent-target",
                        targets=[],
                        max_tokens=target_token_limit,
                        max_cost_usd=target_cost_limit,
                    )
                ]
                if include_target
                else []
            )
        ),
        policy=_Policy(),
        event_bus=event_bus,
        orchestrator=runtime,
        mode=mode,
    )
    return storage, service, runtime, parent.id, sink


class _UsageProvider:
    provider_id = "fake"

    def __init__(
        self,
        *,
        input_tokens: int | None = 3,
        output_tokens: int | None = 2,
        latency_ms: int = 7,
        cost_usd: float | None = 0.25,
        cost_status: Literal["reported", "estimated", "unavailable"] = "reported",
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.cost_usd = cost_usd
        self.cost_status: Literal["reported", "estimated", "unavailable"] = cost_status

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text="child result",
            decision=ModelDecision(action="call", estimated_tokens=0),
            token_usage=cast(
                dict[str, int],
                {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                },
            ),
            latency_ms=self.latency_ms,
            cost_usd=self.cost_usd,
            cost_status=self.cost_status,
        )


async def _record_usage(
    *,
    storage: SQLAlchemyStorage,
    service: DelegationService,
    run_id: str,
    agent_id: str,
    usage_call_id: str,
    provider: _UsageProvider,
    expect_failure: bool = False,
) -> None:
    usage_service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=cast(Any, service)._event_bus,
    )
    invocation = usage_service.complete(
        ModelRequest(provider="fake", prompt="durable usage", max_output_tokens=2),
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id=agent_id,
            request_id="request-a",
            trace_id="trace-parent",
        ),
        usage_call_id=usage_call_id,
    )
    if expect_failure:
        with pytest.raises(ModelProviderInvocationError):
            await invocation
    else:
        await invocation


def _request(parent_run_id: str, **updates: object) -> DelegationRequest:
    payload: dict[str, object] = {
        "parent_run_id": parent_run_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "child_input": {"prompt": "delegate safely"},
        "idempotency_key": "delegation-key",
        "request_id": "request-a",
    }
    payload.update(updates)
    return DelegationRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_local_delegate_replays_one_child_and_holds_unknown_budget(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    try:
        first = await service.delegate(_request(parent_run_id), identity=_identity())
        replay = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            child = await uow.runs.get(first.child_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            reservation = await uow.delegations.get_reservation(first.delegation_id)
        with pytest.raises(RuntimeError, match="pending evidence blocks terminal"):
            await cast(Any, service)._event_bus.publish(
                tenant_id="tenant-a",
                run_id=parent_run_id,
                agent_id="agent-source",
                user_id="user-a",
                event_type=CanonicalEventType.RUN_COMPLETED,
                payload={"status": "completed"},
                terminal=True,
                visibility="public",
                trace_id="trace-parent",
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert replay == first
    assert runtime.calls == 1
    assert child is not None and child.parent_run_id == parent_run_id
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]
    assert [event.event_id for event in events] == [
        f"delegation:{first.delegation_id}:claimed",
        f"delegation:{first.delegation_id}:child",
    ]
    assert all(
        event.run_id == parent_run_id
        and event.trace_id == "trace-parent"
        and event.agent_id == "agent-source"
        and event.record_scope == "run"
        and event.visibility == "internal"
        and event.terminal is False
        for event in events
    )
    assert events[0].payload == {
        "delegation_id": first.delegation_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "status": "claimed",
    }
    assert events[1].payload == {
        "delegation_id": first.delegation_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "status": "completed",
        "child_run_id": first.child_run_id,
    }
    assert capacity.outstanding_reserved_event_count == 1
    assert len(claims) == 1
    assert reservation.state == "needs_review"
    assert first.summary is not None
    assert first.summary.budget_status == "incomplete"


@pytest.mark.asyncio
async def test_trustworthy_child_usage_releases_budget_and_final_event(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            reservation = await uow.delegations.get_reservation(result.delegation_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert result.status == "completed"
    assert result.summary is not None
    assert result.summary.input_tokens == 3
    assert result.summary.output_tokens == 2
    assert result.summary.cost_usd == 0.25
    assert result.summary.latency_ms == 7
    assert result.summary.budget_status == "within_budget"
    assert reservation.state == "settled"
    assert capacity.outstanding_reserved_event_count == 0
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
        "delegation.completed",
    ]
    assert [event.event_id for event in events] == [
        f"delegation:{result.delegation_id}:claimed",
        f"delegation:{result.delegation_id}:child",
        f"delegation:{result.delegation_id}:final",
    ]
    assert all(
        event.run_id == parent_run_id
        and event.trace_id == "trace-parent"
        and event.agent_id == "agent-source"
        and event.record_scope == "run"
        and event.visibility == "internal"
        and event.terminal is False
        for event in events
    )
    assert events[-1].payload == {
        "delegation_id": result.delegation_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "status": "completed",
        "summary": result.summary.to_payload(),
    }


@pytest.mark.asyncio
async def test_unlimited_parent_preserves_finite_target_cost_ceiling(
    tmp_path: Path,
) -> None:
    """parent 无限不等于 target 无限；有限 target ceiling 必须预约并参与结算。"""

    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        source_cost_limit=None,
        target_cost_limit=1.0,
        usage_cost_usd=2.0,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            reservation = await uow.delegations.get_reservation(result.delegation_id)
    finally:
        await storage.dispose()

    assert reservation.reserved_cost_usd == 1.0
    assert reservation.settled_cost_usd == 2.0
    assert result.summary is not None
    assert result.summary.cost_usd == 2.0
    assert result.summary.budget_status == "exceeded"


@pytest.mark.asyncio
async def test_inherit_parent_rejects_when_direct_usage_leaves_insufficient_budget(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=100,
        target_cost_limit=None,
    )
    try:
        await _record_usage(
            storage=storage,
            service=service,
            run_id=parent_run_id,
            agent_id="agent-source",
            usage_call_id="parent-direct-model",
            provider=_UsageProvider(
                input_tokens=90,
                output_tokens=0,
                cost_usd=1.0,
            ),
        )
        with pytest.raises(DelegationError) as captured:
            await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            capacity = await uow.event_capacity.snapshot(parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert runtime.calls == 0
    assert claims == []
    assert capacity.outstanding_reserved_event_count == 0
    assert all(not event.event_type.value.startswith("delegation.") for event in events)


@pytest.mark.asyncio
async def test_finite_parent_cost_rejects_unbounded_target_before_child(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=10,
        target_cost_limit=None,
    )
    try:
        with pytest.raises(DelegationError) as captured:
            await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            capacity = await uow.event_capacity.snapshot(parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert runtime.calls == 0
    assert claims == []
    assert capacity.outstanding_reserved_event_count == 0
    assert events == []


@pytest.mark.asyncio
async def test_mixed_usage_rows_keep_known_token_sum_but_require_review(tmp_path: Path) -> None:
    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
    )
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            await uow.runs.set_status(submitted.child_run_id, RunStatus.RUNNING.value)
            await uow.commit()
        await _record_usage(
            storage=storage,
            service=service,
            run_id=submitted.child_run_id,
            agent_id="agent-target",
            usage_call_id="child-known-model",
            provider=_UsageProvider(input_tokens=3, output_tokens=2),
        )
        await _record_usage(
            storage=storage,
            service=service,
            run_id=submitted.child_run_id,
            agent_id="agent-target",
            usage_call_id="child-unknown-input-model",
            provider=_UsageProvider(input_tokens=None, output_tokens=2),
            expect_failure=True,
        )
        async with storage.uow() as uow:
            unknown = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="child-unknown-input-model",
            )
            assert unknown.result_json is not None
            evidence = unknown.result_json["evidence"]
            await uow.session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.id == unknown.id)
                .values(
                    result_json={
                        **unknown.result_json,
                        "evidence": {
                            **evidence,
                            "input_tokens": None,
                            "output_tokens": 2,
                            "cost_usd": 0.25,
                            "cost_status": "reported",
                        },
                    }
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            await uow.runs.set_status(submitted.child_run_id, RunStatus.COMPLETED.value)
            await uow.commit()
        result = await service.reconcile_child(submitted.child_run_id)
        async with storage.uow() as uow:
            reservation = await uow.delegations.get_reservation(result.delegation_id)
    finally:
        await storage.dispose()

    assert result.summary is not None
    assert result.summary.input_tokens == 3
    assert result.summary.output_tokens == 4
    assert result.summary.budget_status == "incomplete"
    assert reservation.state == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_state", ["started", "result_persisted"])
async def test_pending_usage_row_blocks_delegation_settlement(
    tmp_path: Path,
    pending_state: str,
) -> None:
    """已发布 usage 之外仍有未决行时，只保留已知数值，不得释放 parent 预约。"""

    storage, service, _runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
    )
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            usage_rows = await uow.evidence_outbox.list_for_run(run_id=submitted.child_run_id)
            published = next(row for row in usage_rows if row.operation_kind == "model_usage")
            assert published.result_json is not None
            started = published.result_json["started"]
            await uow.runs.set_status(submitted.child_run_id, RunStatus.RUNNING.value)
            await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=submitted.child_run_id,
                usage_call_id=f"child-pending-{pending_state}",
                event_id=f"model.usage:child-pending-{pending_state}",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=started,
            )
            if pending_state == "result_persisted":
                await uow.evidence_outbox.persist_result(
                    tenant_id="tenant-a",
                    usage_call_id=f"child-pending-{pending_state}",
                    result=published.result_json,
                )
            await uow.runs.set_status(submitted.child_run_id, RunStatus.COMPLETED.value)
            await uow.commit()

        result = await service.reconcile_child(submitted.child_run_id)
        async with storage.uow() as uow:
            reservation = await uow.delegations.get_reservation(result.delegation_id)
            parent_capacity = await uow.event_capacity.snapshot(parent_run_id)
            pending = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=f"child-pending-{pending_state}",
            )
            persisted_pending_state = pending.state
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert result.status == "needs_review"
    assert result.summary is not None
    assert result.summary.input_tokens == 3
    assert result.summary.output_tokens == 2
    assert result.summary.cost_usd is None
    assert result.summary.latency_ms is None
    assert result.summary.budget_status == "incomplete"
    assert reservation.state == "needs_review"
    assert parent_capacity.outstanding_reserved_event_count == 1
    assert persisted_pending_state == pending_state
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]


@pytest.mark.asyncio
async def test_durable_parent_summary_keeps_incomplete_over_exceeded_child(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        trustworthy_usage=True,
        target_token_limit=4,
        target_cost_limit=4.0,
    )
    try:
        exceeded_child = await service.delegate(
            _request(parent_run_id, idempotency_key="delegation-exceeded"),
            identity=_identity(),
        )
        exceeded_result = await service.reconcile_child(exceeded_child.child_run_id)
        runtime.usage_service = None
        incomplete_child = await service.delegate(
            _request(parent_run_id, idempotency_key="delegation-incomplete"),
            identity=_identity(),
        )
        incomplete_result = await service.reconcile_child(incomplete_child.child_run_id)
        parent_summary = await service.get_parent_summary(
            tenant_id="tenant-a",
            parent_run_id=parent_run_id,
        )
    finally:
        await storage.dispose()

    assert runtime.calls == 2
    assert exceeded_result.summary is not None
    assert exceeded_result.summary.budget_status == "exceeded"
    assert incomplete_result.summary is not None
    assert incomplete_result.summary.budget_status == "incomplete"
    assert parent_summary is not None
    assert parent_summary.input_tokens == 3
    assert parent_summary.output_tokens == 2
    assert parent_summary.cost_usd is None
    assert parent_summary.latency_ms is None
    assert parent_summary.budget_status == "incomplete"


@pytest.mark.asyncio
async def test_terminal_parent_rejects_before_delegation_business_state(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    try:
        async with storage.uow() as uow:
            await uow.runs.set_status(parent_run_id, "completed", output={"ok": True})
            await uow.commit()
        await cast(Any, service)._event_bus.publish(
            tenant_id="tenant-a",
            run_id=parent_run_id,
            agent_id="agent-source",
            user_id="user-a",
            event_type=CanonicalEventType.RUN_COMPLETED,
            payload={"status": "completed"},
            terminal=True,
            visibility="public",
            request_id="request-a",
            trace_id="trace-parent",
        )
        with pytest.raises(DelegationError) as captured:
            await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.execution_failed"
    assert runtime.calls == 0
    assert claims == []
    assert pending == []
    assert capacity.outstanding_reserved_event_count == 0
    assert [event.event_type.value for event in events] == ["run.completed"]


@pytest.mark.asyncio
async def test_service_mode_defers_terminal_aggregation_to_worker_recovery(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
    )
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
        recovered = await service.reconcile_child(submitted.child_run_id)
        replayed = await service.reconcile_child(submitted.child_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert submitted.summary is not None
    assert [child.run_id for child in submitted.summary.children] == [submitted.child_run_id]
    assert [child.status for child in submitted.summary.children] == ["completed"]
    assert submitted.summary.input_tokens is None
    assert submitted.summary.output_tokens is None
    assert submitted.summary.cost_usd is None
    assert submitted.summary.latency_ms is None
    assert submitted.summary.budget_status == "incomplete"
    assert recovered.status == "completed"
    assert recovered.summary is not None
    assert recovered.summary.budget_status == "within_budget"
    assert replayed == recovered
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
        "delegation.completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "child_status",
    [RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING],
)
async def test_service_mode_reports_active_child_as_incomplete_parent_summary(
    tmp_path: Path,
    child_status: RunStatus,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        mode="service",
        child_status=child_status,
    )
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
        events = await sink.read(run_id=parent_run_id)
        detail = await get_run_with_orchestrator(
            parent_run_id,
            orchestrator=cast(Any, _ParentDetailOrchestrator()),
            identity=_identity(),
            delegation_service=service,
            request_id="request-detail",
        )
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert submitted.summary is not None
    assert [(child.run_id, child.status) for child in submitted.summary.children] == [
        (submitted.child_run_id, child_status.value)
    ]
    assert submitted.summary.input_tokens is None
    assert submitted.summary.output_tokens is None
    assert submitted.summary.cost_usd is None
    assert submitted.summary.latency_ms is None
    assert submitted.summary.budget_status == "incomplete"
    assert detail.delegation_summary == submitted.summary
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "child_status",
    [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED],
)
async def test_service_mode_reports_unsettled_terminal_child_as_incomplete(
    tmp_path: Path,
    child_status: RunStatus,
) -> None:
    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        child_status=child_status,
    )
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
    finally:
        await storage.dispose()

    assert submitted.summary is not None
    assert [(child.run_id, child.status) for child in submitted.summary.children] == [
        (submitted.child_run_id, child_status.value)
    ]
    assert submitted.summary.input_tokens is None
    assert submitted.summary.output_tokens is None
    assert submitted.summary.cost_usd is None
    assert submitted.summary.latency_ms is None
    assert submitted.summary.budget_status == "incomplete"


@pytest.mark.asyncio
async def test_fast_worker_reconciliation_preserves_delegation_event_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """worker 在 submit 返回前完成 child 时仍必须先发布 child.created 再发布 final。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
    )
    original_submit = runtime.submit_run

    async def submit_and_reconcile_before_return(**kwargs: Any) -> RunResult:
        child = await original_submit(**kwargs)
        await service.reconcile_child(child.run_id)
        return child

    monkeypatch.setattr(runtime, "submit_run", submit_and_reconcile_before_return)
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert result.status == "completed"
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
        "delegation.completed",
    ]


@pytest.mark.asyncio
async def test_parent_without_durable_child_relation_returns_null_summary(tmp_path: Path) -> None:
    storage, service, _runtime, parent_run_id, _sink = await _build_service(tmp_path)
    try:
        summary = await service.get_parent_summary(
            tenant_id="tenant-a",
            parent_run_id=parent_run_id,
        )
        detail = await get_run_with_orchestrator(
            parent_run_id,
            orchestrator=cast(Any, _ParentDetailOrchestrator()),
            identity=_identity(),
            delegation_service=service,
            request_id="request-no-child",
        )
    finally:
        await storage.dispose()

    assert summary is None
    assert detail.delegation_summary is None


@pytest.mark.asyncio
async def test_parent_summary_keeps_completed_and_active_children_until_reconciliation(
    tmp_path: Path,
) -> None:
    """RUN-002 不能把尚未 terminal 聚合的 durable child 误报成不存在。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
        target_token_limit=40,
        target_cost_limit=1.0,
    )
    try:
        completed = await service.delegate(_request(parent_run_id), identity=_identity())
        completed = await service.reconcile_child(completed.child_run_id)
        assert completed.summary is not None
        assert completed.summary.budget_status == "within_budget"

        runtime.child_status = RunStatus.RUNNING
        active = await service.delegate(
            _request(parent_run_id, idempotency_key="delegation-key-active"),
            identity=_identity(),
        )
        summary = await service.get_parent_summary(
            tenant_id="tenant-a",
            parent_run_id=parent_run_id,
        )
    finally:
        await storage.dispose()

    assert active.summary == summary
    assert summary is not None
    assert {child.run_id: child.status for child in summary.children} == {
        completed.child_run_id: "completed",
        active.child_run_id: "running",
    }
    assert summary.input_tokens == 3
    assert summary.output_tokens == 2
    assert summary.cost_usd is None
    assert summary.latency_ms is None
    assert summary.budget_status == "incomplete"


@pytest.mark.asyncio
async def test_parent_summary_uses_durable_child_status_after_aggregation(
    tmp_path: Path,
) -> None:
    """聚合 JSON 只保存数值证据，不能覆盖 durable child 生命周期状态。"""

    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            aggregate = await uow.session.scalar(
                select(DelegationAggregateModel).where(
                    DelegationAggregateModel.delegation_id == result.delegation_id
                )
            )
            assert aggregate is not None
            corrupted = dict(aggregate.summary_json)
            children = [dict(child) for child in corrupted["children"]]
            children[0]["status"] = "waiting"
            corrupted["children"] = children
            await uow.session.execute(
                update(DelegationAggregateModel)
                .where(DelegationAggregateModel.id == aggregate.id)
                .values(summary_json=corrupted)
            )
            await uow.commit()
        summary = await service.get_parent_summary(
            tenant_id="tenant-a",
            parent_run_id=parent_run_id,
        )
    finally:
        await storage.dispose()

    assert summary is not None
    assert [(child.run_id, child.status) for child in summary.children] == [
        (result.child_run_id, "completed")
    ]


@pytest.mark.asyncio
async def test_parent_summary_rejects_aggregate_reservation_state_conflict(
    tmp_path: Path,
) -> None:
    """已结算聚合与仍为 reserved 的预算组合属于损坏状态，必须封闭失败。"""

    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            await uow.session.execute(
                update(DelegationBudgetReservationModel)
                .where(DelegationBudgetReservationModel.delegation_id == result.delegation_id)
                .values(state="reserved")
            )
            await uow.commit()
        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.get_parent_summary(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_sub_micro_cost_round_trips_through_settlement_and_parent_summary(
    tmp_path: Path,
) -> None:
    """合同允许的有限小额 cost 必须在 usage、账本、event 与 RUN-002 中保持一致。"""

    small_cost = 0.000_000_4
    storage, service, _runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        usage_cost_usd=small_cost,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        summary = await service.get_parent_summary(
            tenant_id="tenant-a",
            parent_run_id=parent_run_id,
        )
        async with storage.uow() as uow:
            reservation = await uow.delegations.get_reservation(result.delegation_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert result.summary is not None
    assert result.summary.cost_usd == small_cost
    assert summary == result.summary
    assert reservation.settled_cost_usd == small_cost
    assert events[-1].event_type == CanonicalEventType.DELEGATION_COMPLETED
    assert events[-1].payload is not None
    assert events[-1].payload["summary"]["cost_usd"] == small_cost


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relation_field",
    ["parent_run_id", "agent_id", "trace_id", "idempotency_key"],
)
async def test_reconciliation_rejects_corrupted_child_relation_before_settlement(
    tmp_path: Path,
    relation_field: str,
) -> None:
    """child relation 不可信时不得写 aggregate、结算预算或发布 final evidence。"""

    storage, service, _runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            relation_value = "forged"
            if relation_field == "parent_run_id":
                other_parent = await uow.runs.create(
                    RunCreate(
                        tenant_id="tenant-a",
                        session_id="session-a",
                        agent_id="agent-source",
                        trace_id="trace-other-parent",
                    )
                )
                relation_value = other_parent.id
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == result.child_run_id)
                .values(**{relation_field: relation_value})
            )
            await uow.commit()
        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.reconcile_child(result.child_run_id)
        async with storage.uow() as uow:
            aggregate = await uow.session.scalar(
                select(DelegationAggregateModel).where(
                    DelegationAggregateModel.delegation_id == result.delegation_id
                )
            )
            reservation = await uow.delegations.get_reservation(result.delegation_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert aggregate is None
    assert reservation.state == "reserved"
    assert reservation.settled_input_tokens is None
    assert reservation.settled_output_tokens is None
    assert reservation.settled_cost_usd is None
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper_kind",
    [
        "child_usage_refs",
        "child_trace_refs",
        "top_level_trace_refs",
        "latency_ms",
        "budget_status",
        "evidence_refs",
    ],
)
async def test_parent_summary_rejects_aggregate_evidence_tampering(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    """RUN-002 必须把 aggregate 的公开字段与 durable child evidence 完整对账。"""

    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            aggregate = await uow.session.scalar(
                select(DelegationAggregateModel).where(
                    DelegationAggregateModel.delegation_id == result.delegation_id
                )
            )
            assert aggregate is not None
            summary = dict(aggregate.summary_json)
            children = [dict(child) for child in summary["children"]]
            evidence_refs = list(aggregate.evidence_refs_json)
            if tamper_kind == "child_usage_refs":
                children[0]["usage_evidence_refs"] = ["usage-forged"]
            elif tamper_kind == "child_trace_refs":
                children[0]["trace_refs"] = ["trace-forged"]
            elif tamper_kind == "top_level_trace_refs":
                summary["trace_refs"] = ["trace-forged"]
            elif tamper_kind == "latency_ms":
                summary["latency_ms"] = 999_999
            elif tamper_kind == "budget_status":
                summary["budget_status"] = "exceeded"
            else:
                evidence_refs = ["evidence-forged"]
            summary["children"] = children
            await uow.session.execute(
                update(DelegationAggregateModel)
                .where(DelegationAggregateModel.id == aggregate.id)
                .values(
                    summary_json=summary,
                    evidence_refs_json=evidence_refs,
                )
            )
            await uow.commit()
        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.get_parent_summary(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["before_event_write", "after_event_write"])
async def test_final_event_ack_loss_replays_without_duplicate_or_leaked_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    """final 写前失败和写后确认丢失都必须用同一 event_id 收敛。"""

    storage, service, _runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
    )
    submitted = await service.delegate(_request(parent_run_id), identity=_identity())
    failed = False
    final_event_id = f"delegation:{submitted.delegation_id}:final"

    if failure_point == "before_event_write":
        local_sink = cast(LocalJsonlEventSink, sink)
        original_append = local_sink._append_event_unlocked  # pyright: ignore[reportPrivateUsage]

        def fail_once_before_write(event: Any) -> None:
            nonlocal failed
            if event.event_id == final_event_id and not failed:
                failed = True
                raise OSError("delegation final event write unavailable")
            original_append(event)

        monkeypatch.setattr(local_sink, "_append_event_unlocked", fail_once_before_write)
    else:
        original_mark = EvidenceOutboxRepository.mark_event_published

        async def fail_once_after_write(
            repository: EvidenceOutboxRepository,
            *,
            event_id: str,
        ) -> None:
            nonlocal failed
            if event_id == final_event_id and not failed:
                failed = True
                raise OSError("delegation final event acknowledgement unavailable")
            await original_mark(repository, event_id=event_id)

        monkeypatch.setattr(
            EvidenceOutboxRepository,
            "mark_event_published",
            fail_once_after_write,
        )

    try:
        with pytest.raises(OSError, match="delegation final event"):
            await service.reconcile_child(submitted.child_run_id)
        recovered = await service.reconcile_child(submitted.child_run_id)
        replayed = await service.reconcile_child(submitted.child_run_id)
        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_by_event_id(event_id=final_event_id)
            reservation = await uow.delegations.get_reservation(submitted.delegation_id)
            outbox_state = None if outbox is None else outbox.state
            reservation_state = reservation.state
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    final_events = [event for event in events if event.event_id == final_event_id]
    assert failed is True
    assert recovered == replayed
    assert outbox_state == "published"
    assert reservation_state == "settled"
    assert len(final_events) == 1
    assert final_events[0].event_type == CanonicalEventType.DELEGATION_COMPLETED


@pytest.mark.asyncio
async def test_published_final_event_replay_rejects_corrupted_sink_semantics(
    tmp_path: Path,
) -> None:
    """outbox 已 published 也必须复核同 event_id 的稳定事件语义。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        local_sink = cast(LocalJsonlEventSink, sink)
        events = await local_sink.read(run_id=parent_run_id)
        final_event_id = f"delegation:{result.delegation_id}:final"
        corrupted_events: list[CanonicalEvent] = []
        for event in events:
            if event.event_id != final_event_id:
                corrupted_events.append(event)
                continue
            payload = dict(event.payload or {})
            payload["status"] = "running"
            corrupted_events.append(event.model_copy(update={"payload": payload}))
        local_sink.path.write_bytes(
            b"".join(canonical_event_bytes(event) + b"\n" for event in corrupted_events)
        )

        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.reconcile_child(result.child_run_id)
        replayed = await local_sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert len([event for event in replayed if event.event_id == final_event_id]) == 1
    assert (
        next(event for event in replayed if event.event_id == final_event_id).payload
        == next(event for event in corrupted_events if event.event_id == final_event_id).payload
    )


@pytest.mark.asyncio
async def test_published_final_event_replay_restores_missing_sink_event(tmp_path: Path) -> None:
    """outbox 已 published 但 sink evidence 缺失时，重放必须受控恢复同一事件。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        local_sink = cast(LocalJsonlEventSink, sink)
        final_event_id = f"delegation:{result.delegation_id}:final"
        retained = [
            event
            for event in await local_sink.read(run_id=parent_run_id)
            if event.event_id != final_event_id
        ]
        local_sink.path.write_bytes(
            b"".join(canonical_event_bytes(event) + b"\n" for event in retained)
        )

        recovered = await service.reconcile_child(result.child_run_id)
        replayed = await local_sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    final_events = [event for event in replayed if event.event_id == final_event_id]
    assert recovered == result
    assert runtime.calls == 1
    assert len(final_events) == 1
    assert final_events[0].event_type == CanonicalEventType.DELEGATION_COMPLETED


@pytest.mark.asyncio
async def test_corrupted_child_usage_scope_fails_closed_without_releasing_budget(
    tmp_path: Path,
) -> None:
    storage, service, _runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        mode="service",
    )
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            usage_rows = await uow.evidence_outbox.list_for_run(run_id=submitted.child_run_id)
            usage = next(row for row in usage_rows if row.operation_kind == "model_usage")
            assert usage.result_json is not None
            corrupted = {
                **usage.result_json,
                "evidence": {
                    **usage.result_json["evidence"],
                    "agent_id": "forged-agent",
                },
            }
            await uow.session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.id == usage.id)
                .values(result_json=corrupted)
            )
            await uow.commit()
        result = await service.reconcile_child(submitted.child_run_id)
        async with storage.uow() as uow:
            reservation = await uow.delegations.get_reservation(result.delegation_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert result.status == "needs_review"
    assert result.summary is not None
    assert result.summary.budget_status == "incomplete"
    assert result.summary.children[0].usage_evidence_refs == []
    assert reservation.state == "needs_review"
    assert capacity.outstanding_reserved_event_count == 1
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]


@pytest.mark.asyncio
async def test_failed_child_records_closed_error_and_is_not_reexecuted(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        trustworthy_usage=True,
        child_status=RunStatus.FAILED,
    )
    try:
        first = await service.delegate(_request(parent_run_id), identity=_identity())
        replay = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claim = await uow.delegations.get(first.delegation_id)
            reservation = await uow.delegations.get_reservation(first.delegation_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert replay == first
    assert first.status == "failed"
    assert first.summary is not None
    assert first.summary.children[0].status == "failed"
    assert claim is not None and claim.error_code == "delegation.execution_failed"
    assert reservation.state == "settled"
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
        "delegation.failed",
    ]
    assert events[-1].payload is not None
    assert events[-1].event_id == f"delegation:{first.delegation_id}:final"
    assert events[-1].payload == {
        "delegation_id": first.delegation_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "status": "failed",
        "error_code": "delegation.execution_failed",
        "summary": first.summary.to_payload(),
    }
    assert "child_run_id" not in events[-1].payload
    assert events[-1].visibility == "internal"
    assert events[-1].terminal is False


@pytest.mark.asyncio
async def test_policy_deny_has_zero_delegation_business_side_effects(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    try:
        with pytest.raises(DelegationError) as captured:
            await service.delegate(
                _request(parent_run_id),
                identity=_identity(permissions=[]),
            )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.policy_denied"
    assert runtime.calls == 0
    assert claims == []
    assert events == []


@pytest.mark.asyncio
async def test_cross_tenant_parent_denies_before_claim(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    try:
        with pytest.raises(DelegationError) as captured:
            await service.delegate(
                _request(parent_run_id),
                identity=_identity().model_copy(update={"tenant_id": "tenant-b"}),
            )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.policy_denied"
    assert runtime.calls == 0
    assert claims == []
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_update",
    [
        {"session_id": "session-forged"},
        {"user_id": "user-forged"},
    ],
    ids=["different-session", "different-user"],
)
async def test_parent_ownership_denies_before_delegation_side_effects(
    tmp_path: Path,
    identity_update: dict[str, str],
) -> None:
    """同租户调用也必须由 durable session 证明 parent ownership。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    try:
        with pytest.raises(DelegationError) as captured:
            await service.delegate(
                _request(parent_run_id),
                identity=_identity().model_copy(update=identity_update),
            )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            runs = await uow.runs.list_for_tenant("tenant-a")
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.policy_denied"
    assert runtime.calls == 0
    assert claims == []
    assert [run.id for run in runs] == [parent_run_id]
    assert events == []


@pytest.mark.asyncio
async def test_committed_claim_recovery_launches_one_child_without_re_reserving(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        async with storage.uow() as uow:
            claim = await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    tenant_id="tenant-a",
                    parent_run_id=parent_run_id,
                    source_agent_id="agent-source",
                    target_agent_id="agent-target",
                    idempotency_key=request.idempotency_key,
                    request_hash=delegation_request_hash(request, identity=identity),
                    budget_intent="inherit_parent",
                    child_input=request.child_input,
                    identity=identity.to_payload(),
                    trace_id="trace-parent",
                    request_id=request.request_id,
                    parent_token_limit=100,
                    requested_token_reservation=100,
                    parent_cost_limit=10.0,
                    requested_cost_reservation=10.0,
                )
            )
            await uow.commit()
        recovered = await service.delegate(request, identity=identity)
        replay = await service.delegate(request, identity=identity)
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert claim.created is True
    assert recovered == replay
    assert recovered.delegation_id == claim.delegation.id
    assert runtime.calls == 1
    assert len(claims) == 1
    assert capacity.outstanding_reserved_event_count == 1
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift_kind",
    [
        "claim_source",
        "claim_target",
        "claim_input",
        "claim_identity",
        "claim_budget",
        "claim_trace",
        "claim_registry",
        "claim_capacity",
        "reservation_parent",
        "outbox_parent",
        "outbox_event_id",
        "outbox_result_target",
    ],
)
async def test_claim_replay_rejects_durable_operation_drift_before_child(
    tmp_path: Path,
    drift_kind: str,
) -> None:
    """同 hash 重放必须恢复首次 operation，不得接受 claim 配套状态漂移。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        async with storage.uow() as uow:
            parent = await uow.runs.get(parent_run_id)
            assert parent is not None
            other_parent = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id=parent.session_id,
                    agent_id="agent-source",
                    trace_id="trace-replay-other",
                )
            )
            claim = await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    tenant_id="tenant-a",
                    parent_run_id=parent_run_id,
                    source_agent_id="agent-source",
                    target_agent_id="agent-target",
                    idempotency_key=request.idempotency_key,
                    request_hash=delegation_request_hash(request, identity=identity),
                    budget_intent="inherit_parent",
                    child_input=request.child_input,
                    identity=identity.to_payload(),
                    trace_id="trace-parent",
                    request_id=request.request_id,
                    parent_token_limit=100,
                    requested_token_reservation=100,
                    parent_cost_limit=10.0,
                    requested_cost_reservation=10.0,
                )
            )
            if drift_kind.startswith("claim_"):
                field, value = {
                    "claim_source": ("source_agent_id", "agent-target"),
                    "claim_target": ("target_agent_id", "agent-source"),
                    "claim_input": ("child_input_json", {"prompt": "drifted"}),
                    "claim_identity": (
                        "identity_json",
                        identity.model_copy(update={"user_id": "user-forged"}).to_payload(),
                    ),
                    "claim_budget": ("budget_intent", "drifted"),
                    "claim_trace": ("trace_id", "trace-forged"),
                    "claim_registry": ("event_registry_version", "0"),
                    "claim_capacity": ("reserved_event_count", 1),
                }[drift_kind]
                await uow.session.execute(
                    update(AgentDelegationModel)
                    .where(AgentDelegationModel.id == claim.delegation.id)
                    .values(**{field: value})
                )
            elif drift_kind == "reservation_parent":
                await uow.session.execute(
                    update(DelegationBudgetReservationModel)
                    .where(DelegationBudgetReservationModel.delegation_id == claim.delegation.id)
                    .values(parent_run_id=other_parent.id)
                )
            elif drift_kind == "outbox_parent":
                await uow.session.execute(
                    update(RunEvidenceOutboxModel)
                    .where(
                        RunEvidenceOutboxModel.group_id
                        == f"delegation:{claim.delegation.id}:evidence"
                    )
                    .values(run_id=other_parent.id)
                )
            elif drift_kind == "outbox_event_id":
                await uow.session.execute(
                    update(RunEvidenceOutboxModel)
                    .where(
                        RunEvidenceOutboxModel.event_id
                        == f"delegation:{claim.delegation.id}:claimed"
                    )
                    .values(event_id=f"delegation:{claim.delegation.id}:drifted")
                )
            else:
                row = await uow.session.scalar(
                    select(RunEvidenceOutboxModel).where(
                        RunEvidenceOutboxModel.event_id
                        == f"delegation:{claim.delegation.id}:claimed"
                    )
                )
                assert row is not None and row.result_json is not None
                row.result_json = {**row.result_json, "target_agent_id": "agent-source"}
            await uow.commit()

        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.delegate(request, identity=identity)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 0
    assert events == []


@pytest.mark.asyncio
async def test_committed_claim_recovery_rejects_changed_session_owner(
    tmp_path: Path,
) -> None:
    """恢复授权必须重新绑定 durable session owner，不能只信 claim 内 identity。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        async with storage.uow() as uow:
            await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    tenant_id="tenant-a",
                    parent_run_id=parent_run_id,
                    source_agent_id="agent-source",
                    target_agent_id="agent-target",
                    idempotency_key=request.idempotency_key,
                    request_hash=delegation_request_hash(request, identity=identity),
                    budget_intent="inherit_parent",
                    child_input=request.child_input,
                    identity=identity.to_payload(),
                    trace_id="trace-parent",
                    request_id=request.request_id,
                    parent_token_limit=100,
                    requested_token_reservation=100,
                    parent_cost_limit=10.0,
                    requested_cost_reservation=10.0,
                )
            )
            await uow.session.execute(
                update(SessionModel)
                .where(SessionModel.id == identity.session_id)
                .values(user_id="user-forged")
            )
            await uow.commit()
        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.recover_pending_for_parent(parent_run_id=parent_run_id)
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant("tenant-a")
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 0
    assert [run.id for run in runs] == [parent_run_id]
    assert events == []


@pytest.mark.asyncio
async def test_recovery_entrypoint_finishes_committed_claim_without_parent_reexecution(
    tmp_path: Path,
) -> None:
    """parent executor 已退出后，durable claim 仍必须能独立恢复 child。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        async with storage.uow() as uow:
            claim = await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    tenant_id="tenant-a",
                    parent_run_id=parent_run_id,
                    source_agent_id="agent-source",
                    target_agent_id="agent-target",
                    idempotency_key=request.idempotency_key,
                    request_hash=delegation_request_hash(request, identity=identity),
                    budget_intent="inherit_parent",
                    child_input=request.child_input,
                    identity=identity.to_payload(),
                    trace_id="trace-parent",
                    request_id=request.request_id,
                    parent_token_limit=100,
                    requested_token_reservation=100,
                    parent_cost_limit=10.0,
                    requested_cost_reservation=10.0,
                )
            )
            await uow.commit()

        recovered = await service.recover_pending_for_parent(parent_run_id=parent_run_id)
        replayed = await service.recover_pending_for_parent(parent_run_id=parent_run_id)
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert claim.created is True
    assert recovered == 1
    assert replayed == 0
    assert runtime.calls == 1
    assert len(claims) == 1
    assert claims[0].child_run_id is not None
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]


@pytest.mark.asyncio
async def test_second_key_budget_denial_preserves_first_operation(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    try:
        first = await service.delegate(_request(parent_run_id), identity=_identity())
        with pytest.raises(DelegationError) as captured:
            await service.delegate(
                _request(parent_run_id, idempotency_key="delegation-key-b"),
                identity=_identity(),
            )
        replay = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            capacity = await uow.event_capacity.snapshot(parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert replay == first
    assert runtime.calls == 1
    assert len(claims) == 1
    assert capacity.outstanding_reserved_event_count == 1
    assert len(events) == 2


@pytest.mark.asyncio
async def test_pre_child_deterministic_failure_releases_once_and_replays_failure(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        launch_error=True,
    )
    try:
        for _ in range(2):
            with pytest.raises(DelegationError) as captured:
                await service.delegate(_request(parent_run_id), identity=_identity())
            assert captured.value.code == "delegation.execution_failed"
        runtime.launch_error = False
        recovered_budget = await service.delegate(
            _request(parent_run_id, idempotency_key="after-release"),
            identity=_identity(),
        )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            failed_claim = next(
                claim for claim in claims if claim.idempotency_key == "delegation-key"
            )
            reservation = await uow.delegations.get_reservation(failed_claim.id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            runs = await uow.runs.list_for_tenant("tenant-a")
            failed_group = await uow.evidence_outbox.ordered_group(
                group_id=f"delegation:{failed_claim.id}:evidence"
            )
            failed_group_states = [item.state for item in failed_group]
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 2
    assert len(claims) == 2 and failed_claim.status == "failed"
    assert recovered_budget.status == "needs_review"
    assert reservation.state == "released"
    assert failed_group_states == ["published", "cancelled", "published"]
    assert capacity.outstanding_reserved_event_count == 1
    assert len(runs) == 2
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.failed",
        "delegation.claimed",
        "delegation.child.created",
    ]
    assert [event.event_id for event in events[:2]] == [
        f"delegation:{failed_claim.id}:claimed",
        f"delegation:{failed_claim.id}:final",
    ]
    assert events[1].payload == {
        "delegation_id": failed_claim.id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "status": "failed",
        "error_code": "delegation.execution_failed",
    }
    assert events[1].visibility == "internal"
    assert events[1].terminal is False


@pytest.mark.asyncio
async def test_capacity_exhaustion_rejects_before_child_or_business_event(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        database_events=True,
    )
    try:
        async with storage.uow() as uow:
            await uow.event_capacity.reconcile_local_prefix(
                run_id=parent_run_id,
                highest_persisted_seq=MAX_EVENT_SEQ - 3,
            )
            await uow.commit()
        with pytest.raises(DelegationError) as captured:
            await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            runs = await uow.runs.list_for_tenant("tenant-a")
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "event.sequence_exhausted"
    assert runtime.calls == 0
    assert claims == []
    assert len(runs) == 1
    assert pending == []
    assert events == []
    assert capacity.highest_persisted_seq == MAX_EVENT_SEQ - 3
    assert capacity.outstanding_reserved_event_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_updates", "source_targets", "include_target", "expected_code"),
    [
        ({"target_agent_id": "agent-source"}, ["agent-source"], True, "delegation.cycle_detected"),
        ({"target_agent_id": "missing"}, ["missing"], False, "delegation.target_not_found"),
        ({}, ["agent-other"], True, "delegation.edge_denied"),
    ],
)
async def test_stateless_authorization_denies_before_claim(
    tmp_path: Path,
    request_updates: dict[str, object],
    source_targets: list[str],
    include_target: bool,
    expected_code: str,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        source_targets=source_targets,
        include_target=include_target,
    )
    try:
        with pytest.raises(DelegationError) as captured:
            await service.delegate(
                _request(parent_run_id, **request_updates),
                identity=_identity(),
            )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == expected_code
    assert runtime.calls == 0
    assert claims == []
    assert events == []


@pytest.mark.asyncio
async def test_single_level_depth_denies_child_parent_delegation(tmp_path: Path) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        delegated_parent=True,
    )
    try:
        with pytest.raises(DelegationError) as captured:
            await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.depth_exceeded"
    assert runtime.calls == 0
    assert claims == []
    assert events == []
