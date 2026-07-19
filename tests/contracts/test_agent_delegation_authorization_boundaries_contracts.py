"""Agent 委派冻结边、租户边界与已提交 claim 恢复合同。"""

import hashlib
import json
from decimal import Decimal

from tests.contracts.agent_delegation_service_runtime_test_support import (
    _SharedBudgetRuntimeFixture,
)

# 复用服务合同的夹具与类型，不导入其他 test_*，避免 pytest 重复收集。
from tests.contracts.test_agent_delegation_service_contracts import (
    AgentRegistry,
    Any,
    DelegationClaimCreate,
    DelegationError,
    Path,
    cast,
    delegation_request_hash,
    pytest,
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

from agent_harness.delegation.models import delegation_relation_id, delegation_request_bytes
from agent_harness.storage.shared_budget_models import ParentBudgetLedgerModel


@pytest.mark.asyncio
@pytest.mark.parametrize("rehash", [False, True], ids=["hash-mismatch", "missing-targets"])
async def test_corrupted_frozen_edge_catalog_fails_closed(
    tmp_path: Path,
    rehash: bool,
) -> None:
    """缺失显式 edge 或 snapshot hash 不匹配时都不得由 agents 反推授权。"""

    storage, service, runtime, parent_run_id, _sink = await _build_service(tmp_path)
    try:
        async with storage.uow() as uow:
            ledger = await uow.session.get(
                ParentBudgetLedgerModel,
                ("tenant-a", parent_run_id),
            )
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            owner = dict(snapshot["owner"])
            owner.pop("delegation_targets")
            snapshot["owner"] = owner
            ledger.snapshot_json = snapshot
            if rehash:
                ledger.snapshot_hash = hashlib.sha256(
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
            await uow.commit()
        with pytest.raises(DelegationError) as denied:
            await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()

    assert denied.value.code == "delegation.edge_denied"
    assert runtime.calls == 0
    assert claims == []


@pytest.mark.asyncio
async def test_registry_budget_reload_cannot_change_frozen_target_reservation(
    tmp_path: Path,
) -> None:
    """新 delegation 也只能使用 root snapshot 中冻结的 target ceiling。"""

    storage, service, _runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=60,
        target_cost_limit=4.0,
    )
    try:
        cast(Any, service)._registry = AgentRegistry(
            [_descriptor("agent-source", targets=[], max_tokens=1)]
        )
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            reservation = await uow.delegations.get_reservation(result.delegation_id)
    finally:
        await storage.dispose()

    assert reservation.reserved_tokens == 60
    assert reservation.reserved_cost_usd == 4.0


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
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            snapshot = await uow.shared_budget.get_tree_snapshot("tenant-a", parent_run_id)
            assert ledger is not None and snapshot is not None
            delegation_id = delegation_relation_id(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
                idempotency_key=request.idempotency_key,
            )
            budget_identity = _SharedBudgetRuntimeFixture().delegation_identity(
                tenant_id="tenant-a",
                canonical_request_bytes=delegation_request_bytes(request, identity=identity),
                parent_run_id=parent_run_id,
                source_agent_id="agent-source",
                target_agent_id="agent-target",
                delegation_id=delegation_id,
                idempotency_key=request.idempotency_key,
                tree_snapshot_id=ledger.snapshot_id,
                snapshot=snapshot,
                trusted_token_bound=100,
                trusted_cost_bound=Decimal("10.0"),
            )
            claim = await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    delegation_id=delegation_id,
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
                    budget_identity=budget_identity,
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
