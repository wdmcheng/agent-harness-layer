"""Canonical run trace 的租户边界、恢复与 evidence 传播合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.run_trace_contract_helpers import persisted_event_bus, sqlite_dsn
from tests.contracts.runtime_contract_helpers import FakeContractExecutor

from agent_harness.approvals import ApprovalService
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.events import LocalJsonlEventSink
from agent_harness.events.types import CanonicalEvent, CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.policy import InputGuardrail, PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import (
    InMemoryRunQueue,
    RunOrchestrator,
    RunTraceConflict,
    RunTraceValidationError,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.run_trace_gate import RunTraceScopeConflict
from app.api.routes.runs import RunCreateRequest, create_run_with_orchestrator


@pytest.mark.asyncio
async def test_trace_preflight_is_tenant_scoped_while_claim_remains_global(tmp_path: Path) -> None:
    """跨 tenant 预检不返回他人 binding，最终唯一 claim 仍稳定拒绝复用。"""

    dsn = sqlite_dsn(tmp_path / "tenant-claim.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    owner = IdentityContext.local_default(session_id="owner-session")
    other = IdentityContext(
        tenant_id="other-tenant",
        user_id="other-user",
        session_id="other-session",
    )
    try:
        first = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            identity=owner,
            trace_id="globally-bound-trace",
        )
        async with storage.uow() as uow:
            assert (
                await uow.runs.get_trace_binding_root(
                    tenant_id=owner.tenant_id,
                    trace_id="globally-bound-trace",
                )
                == first.run_id
            )
            assert (
                await uow.runs.get_trace_binding_root(
                    tenant_id=other.tenant_id,
                    trace_id="globally-bound-trace",
                )
                is None
            )
        with pytest.raises(RunTraceConflict):
            await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                identity=other,
                trace_id="globally-bound-trace",
            )
        async with storage.uow() as uow:
            other_runs = await uow.runs.list_for_tenant(other.tenant_id)
    finally:
        await storage.dispose()

    assert other_runs == []
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.asyncio
async def test_cross_tenant_trace_conflict_precedes_guardrail_audit(tmp_path: Path) -> None:
    """全局 trace 冲突必须在 guardrail audit 及 run/event 副作用前失败。"""

    dsn = sqlite_dsn(tmp_path / "tenant-preflight-side-effects.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    owner = IdentityContext.local_default(session_id="trace-owner-session")
    other = IdentityContext(
        tenant_id="trace-other-tenant",
        user_id="trace-other-user",
        session_id="trace-other-session",
    )
    audit = AuditService(storage=storage)
    guardrail = InputGuardrail(
        policy=PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit),
        audit=audit,
    )
    try:
        owner_run = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            identity=owner,
            trace_id="shared-global-trace",
        )
        with pytest.raises(RunTraceConflict):
            await create_run_with_orchestrator(
                RunCreateRequest(
                    agent_id="fake-agent",
                    input={"prompt": "ordinary input"},
                    idempotency_key="other-tenant-request",
                ),
                orchestrator=orchestrator,
                identity=other,
                input_guardrail=guardrail,
                request_id="other-tenant-request",
                trace_id="shared-global-trace",
            )
        async with storage.uow() as uow:
            other_runs = await uow.runs.list_for_tenant(other.tenant_id)
            other_audits = await uow.audit_logs.list_for_tenant(other.tenant_id)
    finally:
        await storage.dispose()

    assert other_runs == []
    assert other_audits == []
    assert [event.event_id for event in await sink.read(run_id=owner_run.run_id)]
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.asyncio
async def test_service_submit_persists_trace_for_worker_recovery(tmp_path: Path) -> None:
    """service queue message只携带 ref，worker 从持久化 context 恢复 trace。"""

    dsn = sqlite_dsn(tmp_path / "trace-service.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    queue = InMemoryRunQueue()
    identity = IdentityContext.local_default()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        queue=queue,
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    try:
        submitted = await orchestrator.submit_run(
            agent_id="fake-agent",
            input={"source_ref": "source://service"},
            idempotency_key="service",
            identity=identity,
            trace_id="trace-service",
        )
        delivery = await queue.pickup(consumer_id="worker")
        assert delivery is not None
        completed = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner",
            workflow_id="workflow",
        )
        async with storage.uow() as uow:
            persisted_trace = await uow.runs.get_trace(submitted.run_id)
    finally:
        await storage.dispose()

    assert completed.status.value == "completed"
    assert persisted_trace == "trace-service"
    assert {event.trace_id for event in await sink.read(run_id=submitted.run_id)} == {
        "trace-service"
    }


@pytest.mark.asyncio
async def test_approval_and_event_bus_cannot_override_or_omit_persisted_trace(
    tmp_path: Path,
) -> None:
    """approval 忽略 caller override；EventBus 在分配 seq/fan-out 前拒绝坏 trace。"""

    dsn = sqlite_dsn(tmp_path / "approval-event.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    artifact_root = tmp_path / "artifacts"
    bus = persisted_event_bus(
        storage,
        sink,
        artifact_store=FileArtifactStore(artifact_root),
    )
    orchestrator = RunOrchestrator(storage=storage, event_bus=bus)
    approvals = ApprovalService(storage=storage, event_bus=bus, orchestrator=orchestrator)
    identity = IdentityContext.local_default()
    try:
        waiting = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            checkpoint_state={"reason": "review"},
            identity=identity,
            trace_id="trace-canonical",
        )
        record = await approvals.require_approval(
            actor=identity,
            run_id=waiting.run_id,
            agent_id="fake-agent",
            action="write",
            resource="file:a",
            reason="review",
            resume_token=waiting.resume_token,
            trace_id="trace-caller-must-not-win",
        )
        assert record.trace_id == "trace-canonical"
        assert {event.trace_id for event in await sink.read(run_id=waiting.run_id)} == {
            "trace-canonical"
        }
        before = len(events_path.read_text(encoding="utf-8").splitlines())
        with pytest.raises(RunTraceValidationError):
            await bus.publish(
                tenant_id="default",
                run_id=waiting.run_id,
                event_type=CanonicalEventType.POLICY_DECISION,
                trace_id=None,
            )
        with pytest.raises(RunTraceValidationError):
            await bus.publish(
                tenant_id="default",
                run_id=waiting.run_id,
                event_type=CanonicalEventType.POLICY_DECISION,
                trace_id=" invalid",
            )
        for tenant_id, run_id, trace_id in (
            ("default", waiting.run_id, "trace-other"),
            ("other-tenant", waiting.run_id, "trace-canonical"),
            ("default", "missing-run", "trace-canonical"),
        ):
            with pytest.raises(RunTraceScopeConflict):
                await bus.publish(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    event_type=CanonicalEventType.POLICY_DECISION,
                    payload={"large": "x" * 9000},
                    trace_id=trace_id,
                )
        with pytest.raises(RunTraceScopeConflict):
            await sink.write(
                CanonicalEvent(
                    tenant_id="default",
                    run_id=waiting.run_id,
                    event_type=CanonicalEventType.POLICY_DECISION,
                    seq=99,
                    trace_id="trace-other",
                )
            )
        with pytest.raises(ValueError, match="must be public"):
            await bus.publish(
                tenant_id="default",
                run_id=waiting.run_id,
                event_type=CanonicalEventType.RUN_FAILED,
                trace_id="trace-canonical",
                terminal=True,
                visibility="internal",
            )
        with pytest.raises(ValueError, match="record_scope must be run or non_run"):
            await bus.publish(
                tenant_id="telemetry",
                run_id="telemetry",
                event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
                trace_id=None,
                record_scope=cast(Any, "other"),
            )
        assert len(events_path.read_text(encoding="utf-8").splitlines()) == before
        telemetry = await bus.publish(
            tenant_id="telemetry",
            run_id="telemetry",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            trace_id=None,
            record_scope="non_run",
        )
        assert telemetry.record_scope == "non_run"
        assert telemetry.trace_id is None
        assert len(events_path.read_text(encoding="utf-8").splitlines()) == before + 1
        assert not artifact_root.exists()
    finally:
        await storage.dispose()
