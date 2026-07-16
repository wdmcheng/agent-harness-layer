"""Agent 委派 parent 摘要、状态与事件顺序合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_service_contracts import (
    Any as Any,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationAggregateModel as DelegationAggregateModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationBudgetReservationModel as DelegationBudgetReservationModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationError as DelegationError,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    RunResult as RunResult,
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
    _ParentDetailOrchestrator as _ParentDetailOrchestrator,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _request as _request,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    cast as cast,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    get_run_with_orchestrator as get_run_with_orchestrator,
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
