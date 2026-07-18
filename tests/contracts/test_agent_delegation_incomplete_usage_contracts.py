"""Agent 委派不完整用量、结算阻断与 terminal parent 合同测试。"""

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
    EvidenceOperationKind as EvidenceOperationKind,
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
from tests.contracts.test_agent_delegation_service_contracts import (
    update as update,
)


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
async def test_exceeded_child_fences_new_delegation_from_shared_tree(
    tmp_path: Path,
) -> None:
    storage, service, runtime, parent_run_id, _sink = await _build_service(
        tmp_path,
        mode="service",
        trustworthy_usage=True,
        target_token_limit=13,
        target_cost_limit=4.0,
        usage_output_tokens=20,
    )
    try:
        exceeded_child = await service.delegate(
            _request(parent_run_id, idempotency_key="delegation-exceeded"),
            identity=_identity(),
        )
        exceeded_result = await service.reconcile_child(exceeded_child.child_run_id)
        runtime.usage_service = None
        with pytest.raises(DelegationError) as rejected:
            await service.delegate(
                _request(parent_run_id, idempotency_key="delegation-incomplete"),
                identity=_identity(),
            )
        parent_summary = await service.get_parent_summary(
            tenant_id="tenant-a",
            parent_run_id=parent_run_id,
        )
    finally:
        await storage.dispose()

    assert runtime.calls == 1
    assert rejected.value.code == "delegation.budget_exceeded"
    assert exceeded_result.summary is not None
    assert exceeded_result.summary.budget_status == "exceeded"
    assert parent_summary is not None
    assert parent_summary.input_tokens == 3
    assert parent_summary.output_tokens == 20
    assert parent_summary.cost_usd == 0.25
    assert parent_summary.latency_ms == 7
    assert parent_summary.budget_status == "exceeded"


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
