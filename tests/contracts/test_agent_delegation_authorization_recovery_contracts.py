"""Agent 委派授权、租户边界与已提交 claim 恢复合同测试。"""

from __future__ import annotations

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
    RunEvidenceOutboxModel as RunEvidenceOutboxModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    RunStatus as RunStatus,
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
    update as update,
)


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
