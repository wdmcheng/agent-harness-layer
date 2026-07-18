"""Agent 委派 durable drift、claim 恢复与预算竞争合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_service_contracts import (
    AgentDelegationModel as AgentDelegationModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationBudgetReservationModel as DelegationBudgetReservationModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationClaimCreate as DelegationClaimCreate,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationError as DelegationError,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    RunEvidenceOutboxModel as RunEvidenceOutboxModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    SessionModel as SessionModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _build_service as _build_service,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _identity as _identity,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _request as _request,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    delegation_request_hash as delegation_request_hash,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    select as select,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    update as update,
)

from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


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
async def test_recovery_never_relaunches_started_unknown_delegation(tmp_path: Path) -> None:
    """顶层 claim 已 started 且无 child 时只能 needs_review，禁止重放 launcher。"""

    storage, service, runtime, parent_run_id, sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        async with storage.uow() as uow:
            claimed = await uow.delegations.claim_and_reserve(
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
            started = await uow.shared_budget.mark_delegation_started(
                delegation_id=claimed.delegation.id
            )
            await uow.commit()
        assert started is not None and started.replayed is False

        with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
            await service.recover_pending_for_parent(parent_run_id=parent_run_id)

        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            top_claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == claimed.delegation.id
                )
            )
            relation = await uow.delegations.get(claimed.delegation.id)
            ledger_state = None if ledger is None else ledger.state
            claim_state = None if top_claim is None else top_claim.state
            side_effect_state = None if top_claim is None else top_claim.side_effect_state
            child_run_id = None if relation is None else relation.child_run_id
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 0
    assert ledger_state == "needs_review"
    assert claim_state == "needs_review"
    assert side_effect_state == "started"
    assert child_run_id is None
    assert [event.event_type.value for event in events] == ["delegation.claimed"]


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
