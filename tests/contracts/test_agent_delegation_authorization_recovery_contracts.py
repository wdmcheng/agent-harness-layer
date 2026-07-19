"""Agent 委派授权、租户边界与已提交 claim 恢复合同测试。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from tests.contracts.test_agent_delegation_service_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    Any as Any,
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
    RunEvidenceOutboxModel as RunEvidenceOutboxModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _build_service as _build_service,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _descriptor as _descriptor,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _identity as _identity,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _request as _request,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    cast as cast,
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

from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    ParentBudgetLedgerModel,
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
async def test_exact_replay_conflict_and_frozen_edge_precede_current_registry(
    tmp_path: Path,
) -> None:
    """Durable stable key 必须先于 reload 后的 edge/policy 解析。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        first = await service.delegate(request, identity=identity)
        cast(Any, service)._registry = AgentRegistry([_descriptor("agent-source", targets=[])])
        replay = await service.delegate(request, identity=identity)
        with pytest.raises(DelegationError) as conflict:
            await service.delegate(
                _request(parent_run_id, child_input={"prompt": "changed"}),
                identity=identity,
            )
        with pytest.raises(DelegationError) as exhausted:
            await service.delegate(
                _request(parent_run_id, idempotency_key="delegation-new-key"),
                identity=identity,
            )
    finally:
        await storage.dispose()

    assert replay == first
    assert runtime.calls == 1
    assert conflict.value.code == "delegation.idempotency_conflict"
    assert exhausted.value.code == "delegation.budget_exceeded"


@pytest.mark.asyncio
async def test_exact_delegation_replay_precedes_current_snapshot_integrity(
    tmp_path: Path,
) -> None:
    """顶层 durable identity/result 不得被首次执行后的 snapshot 损坏覆盖。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        first = await service.delegate(request, identity=identity)
        async with storage.uow() as uow:
            ledger = await uow.session.get(
                ParentBudgetLedgerModel,
                ("tenant-a", parent_run_id),
            )
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            snapshot["catalog_version"] = "catalog-corrupted-after-delegation"
            ledger.snapshot_json = snapshot
            await uow.commit()

        replayed = await service.delegate(request, identity=identity)
        with pytest.raises(DelegationError) as conflict:
            await service.delegate(
                _request(parent_run_id, child_input={"prompt": "changed after durable claim"}),
                identity=identity,
            )
    finally:
        await storage.dispose()

    assert replayed == first
    assert runtime.calls == 1
    assert conflict.value.code == "delegation.idempotency_conflict"


@pytest.mark.asyncio
async def test_managed_delegation_replay_rejects_missing_top_level_claim(
    tmp_path: Path,
) -> None:
    """共享 ledger 仍存在时，缺失顶层 claim 不得降级成 legacy replay。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(tmp_path)
    request = _request(parent_run_id)
    identity = _identity()
    try:
        first = await service.delegate(request, identity=identity)
        async with storage.uow() as uow:
            ledger = await uow.session.get(
                ParentBudgetLedgerModel,
                ("tenant-a", parent_run_id),
            )
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == first.delegation_id
                )
            )
            assert ledger is not None
            assert claim is not None
            await uow.session.delete(claim)
            await uow.commit()

        with pytest.raises(DelegationError) as captured:
            await service.delegate(request, identity=identity)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.execution_failed"
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_delegation_top_level_claim_persists_versioned_immutable_identity(
    tmp_path: Path,
) -> None:
    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=60,
        target_cost_limit=4.0,
    )
    request = _request(parent_run_id)
    try:
        submitted = await service.delegate(request, identity=_identity())
        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == submitted.delegation_id
                )
            )
            assert claim is not None
            claim_values = {
                "usage_kind": claim.usage_kind,
                "identity_schema_version": claim.identity_schema_version,
                "identity_hash": claim.identity_hash,
                "identity_json": claim.identity_json,
                "request_hash": claim.request_hash,
            }
    finally:
        await storage.dispose()

    assert claim_values["usage_kind"] == "delegation"
    assert claim_values["identity_schema_version"] == "budget-delegation-v1"
    identity_hash = claim_values["identity_hash"]
    assert isinstance(identity_hash, str) and len(identity_hash) == 64
    identity_json = claim_values["identity_json"]
    assert isinstance(identity_json, dict)
    assert identity_json["ownership_kind"] == "delegation"
    assert identity_json["delegation_claim_id"] == submitted.delegation_id
    assert identity_json["run_id"] == parent_run_id
    assert identity_json["source_agent_id"] == "agent-source"
    assert identity_json["target_agent_id"] == "agent-target"
    assert identity_json["provider"] is None
    assert identity_json["model"] is None
    assert identity_json["trusted_token_bound"] == 60
    assert identity_json["trusted_cost_bound"] == "4"
    assert claim_values["request_hash"] == delegation_request_hash(request, identity=_identity())
    assert identity_json["request_fingerprint"] != claim_values["request_hash"]


@pytest.mark.asyncio
async def test_database_rejects_delegation_identity_json_shape_mismatch(tmp_path: Path) -> None:
    """数据库必须逐值拒绝非法 delegation identity JSON。"""

    storage, service, _runtime, parent_run_id, _sink = await _build_service(tmp_path)
    try:
        submitted = await service.delegate(_request(parent_run_id), identity=_identity())
        with pytest.raises(IntegrityError):
            async with storage.uow() as uow:
                claim = await uow.session.scalar(
                    select(BudgetOperationClaimModel).where(
                        BudgetOperationClaimModel.delegation_id == submitted.delegation_id
                    )
                )
                assert claim is not None
                claim.identity_json = {}
                await uow.commit()
        for mode in ("missing", "null", "empty"):
            with pytest.raises(IntegrityError):
                async with storage.uow() as uow:
                    claim = await uow.session.scalar(
                        select(BudgetOperationClaimModel).where(
                            BudgetOperationClaimModel.delegation_id == submitted.delegation_id
                        )
                    )
                    assert claim is not None
                    corrupted = dict(claim.identity_json)
                    if mode == "missing":
                        corrupted.pop("target_route_catalog_digest")
                    else:
                        corrupted["target_route_catalog_digest"] = None if mode == "null" else ""
                    claim.identity_json = corrupted
                    await uow.commit()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_same_request_hash_with_changed_budget_identity_conflicts_before_mutation(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=60,
        target_cost_limit=4.0,
    )
    request = _request(parent_run_id)
    try:
        await service.delegate(request, identity=_identity())
        async with storage.uow() as uow:
            baseline_ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            baseline_capacity = await uow.event_capacity.snapshot(parent_run_id)
            baseline_relations = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
        shared_budget = cast(Any, service)._shared_budget
        shared_budget._fingerprint_key_version = "delegation-contract-v2"
        with pytest.raises(DelegationError) as conflict:
            await service.delegate(request, identity=_identity())
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            relations = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()

    assert conflict.value.code == "delegation.idempotency_conflict"
    assert runtime.calls == 1
    assert ledger == baseline_ledger
    assert capacity == baseline_capacity
    assert [item.id for item in relations] == [item.id for item in baseline_relations]


@pytest.mark.asyncio
async def test_budget_identity_conflict_precedes_corrupted_outbox_integrity(
    tmp_path: Path,
) -> None:
    """稳定预算身份冲突必须先于同一 claim 的后续完整性损坏。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=60,
        target_cost_limit=4.0,
    )
    request = _request(parent_run_id)
    try:
        submitted = await service.delegate(request, identity=_identity())
        async with storage.uow() as uow:
            outbox = await uow.session.scalar(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.group_id
                    == f"delegation:{submitted.delegation_id}:evidence",
                    RunEvidenceOutboxModel.sequence_in_group == 1,
                )
            )
            assert outbox is not None
            await uow.session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.id == outbox.id)
                .values(reserved_event_count=2)
            )
            await uow.commit()
        shared_budget = cast(Any, service)._shared_budget
        shared_budget._fingerprint_key_version = "delegation-contract-v2"
        with pytest.raises(DelegationError) as conflict:
            await service.delegate(request, identity=_identity())
    finally:
        await storage.dispose()

    assert conflict.value.code == "delegation.idempotency_conflict"
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_registry_reload_cannot_add_target_to_existing_root_snapshot(tmp_path: Path) -> None:
    """当前 registry 新增 edge/descriptor 不能扩张旧 root 的 frozen catalog。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        source_targets=[],
    )
    try:
        cast(Any, service)._registry = AgentRegistry(
            [
                _descriptor("agent-source", targets=["agent-target"]),
                _descriptor("agent-target", targets=[]),
            ]
        )
        with pytest.raises(DelegationError) as denied:
            await service.delegate(_request(parent_run_id), identity=_identity())
    finally:
        await storage.dispose()

    assert denied.value.code == "delegation.edge_denied"
    assert runtime.calls == 0
