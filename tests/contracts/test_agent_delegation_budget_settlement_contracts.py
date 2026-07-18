"""Agent 委派预算继承、用量结算与重放合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_service_contracts import (
    Any as Any,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    CanonicalEventType as CanonicalEventType,
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
    _record_usage as _record_usage,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _request as _request,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    _UsageProvider as _UsageProvider,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    cast as cast,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    pytest as pytest,
)


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
async def test_cost_disabled_parent_keeps_target_cost_out_of_budget_accounting(
    tmp_path: Path,
) -> None:
    """owner 关闭 cost 后，target 不能在同一 execution tree 重新启用该维度。"""

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

    assert reservation.reserved_cost_usd is None
    assert reservation.settled_cost_usd == 0.0
    assert result.summary is not None
    assert result.summary.cost_usd is None
    assert result.summary.budget_status == "within_budget"


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
async def test_finite_parent_cost_uses_owner_ceiling_when_target_ceiling_is_null(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, sink = await _build_service(
        tmp_path,
        mode="service",
        target_token_limit=10,
        target_cost_limit=None,
    )
    try:
        result = await service.delegate(_request(parent_run_id), identity=_identity())
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            reservation = await uow.delegations.get_reservation(result.delegation_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
        events = await sink.read(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert [claim.id for claim in claims] == [result.delegation_id]
    assert reservation.reserved_cost_usd == 10.0
    assert capacity.outstanding_reserved_event_count == 1
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]
