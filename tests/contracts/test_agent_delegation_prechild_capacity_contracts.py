"""Agent 委派 child 创建前失败、容量与深度边界合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_service_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationError as DelegationError,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    Path as Path,
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
    pytest as pytest,
)


@pytest.mark.asyncio
async def test_pre_child_deterministic_failure_releases_once_and_replays_failure(
    tmp_path: Path,
) -> None:
    """child 创建前的确定性失败必须释放一次预约并稳定重放失败，后续新 key 仍可安全使用剩余预算。"""

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
    """事件容量耗尽时服务要在 child、claim、outbox 和业务事件之前拒绝委派，维持零副作用。"""

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
    """环、缺失 target 或未授权边必须在持久化 claim 前被纯状态校验拒绝，不能启动 runtime。"""

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
    """已是 child 的运行不得再派生 child，单层深度规则须在任何业务状态变化前执行。"""

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
