"""Agent 委派证据校验、发布重放与对账合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_service_contracts import (
    AgentRunModel as AgentRunModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    Any as Any,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    CanonicalEvent as CanonicalEvent,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationAggregateModel as DelegationAggregateModel,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    DelegationError as DelegationError,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    EvidenceOutboxRepository as EvidenceOutboxRepository,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    RunCreate as RunCreate,
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
    canonical_event_bytes as canonical_event_bytes,
)
from tests.contracts.test_agent_delegation_service_contracts import (
    cast as cast,
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
            """仅在首次 final event 落盘前注入失败，模拟证据尚未写入的故障。"""

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
            """仅在首次 final event 已落盘后丢失确认，模拟 outbox ack 失败窗口。"""

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
