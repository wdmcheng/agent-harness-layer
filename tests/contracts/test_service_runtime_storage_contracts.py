"""Service runtime durable execution 的 migration 与私有 storage 边界合同。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import update

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.audit import AuditService
from agent_harness.events import (
    EventBus,
    LocalJsonlEventSink,
)
from agent_harness.identity import IdentityContext
from agent_harness.runtime import InMemoryRunQueue, RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.approval_records import ApprovalCreate
from agent_harness.storage.models import ApprovalModel
from agent_harness.storage.repositories import RunCreate, SessionCreate


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def test_0012_adds_service_runtime_private_columns_and_terminal_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "service-runtime.db"
    run_migrations(_dsn(db_path))

    with sqlite3.connect(db_path) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()
        run_columns = {
            row[1] for row in connection.execute("pragma table_info(agent_runs)").fetchall()
        }
        approval_columns = {
            row[1] for row in connection.execute("pragma table_info(approvals)").fetchall()
        }
        event_columns = {
            row[1] for row in connection.execute("pragma table_info(canonical_events)").fetchall()
        }
        event_indexes = {
            row[1] for row in connection.execute("pragma index_list(canonical_events)").fetchall()
        }

    assert revision == ("0013a_run_trace_event_hardening",)
    assert {
        "execution_context_json",
        "queue_operation_id",
        "queue_request_id",
        "queue_effective_idempotency_key",
        "queue_enqueue_state",
        "queue_message_id",
        "execution_owner_id",
        "execution_workflow_id",
    } <= run_columns
    assert {
        "resolution_operation_id",
        "resolution_request_id",
        "resolution_reviewer_id",
        "resolution_decision",
        "resolution_request_hash",
        "resolution_comment",
        "resolution_enqueue_state",
        "resolution_message_id",
        "resolution_workflow_owner_id",
        "resolution_workflow_id",
    } <= approval_columns
    assert "envelope_json" in event_columns
    assert "uq_canonical_events_run_terminal" in event_indexes


@pytest.mark.asyncio
async def test_run_repository_keeps_queue_state_private_and_fences_execution(
    tmp_path: Path,
) -> None:
    dsn = _dsn(tmp_path / "queued-run.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-1")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id="tenant-1",
                    user_id="user-1",
                    agent_id="agent-1",
                )
            )
            run = await uow.runs.create_queued(
                RunCreate(
                    tenant_id="tenant-1",
                    session_id=session.id,
                    agent_id="agent-1",
                    idempotency_key="client-key",
                    trace_id="trace-client-key",
                    input={"source_ref": "source://one", "trust_level": "trusted"},
                ),
                execution_context={
                    "identity": {
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                        "roles": ["operator"],
                        "permissions": ["runs:execute"],
                        "auth_method": "api-key",
                    },
                    "request_id": "req-1",
                    "trace_id": "trace-client-key",
                },
                operation_id="run:placeholder:execute",
                request_id="req-1",
                effective_idempotency_key="client-key",
            )
            await uow.commit()

        assert "queue_enqueue_state" not in run.to_payload()
        async with storage.uow() as uow:
            private = await uow.runs.get_execution(run.id)
            pending = await uow.runs.list_pending_enqueue()
            assert private is not None
            # Repository 必须按真实 run id归一 operation，不能保留调用方 placeholder。
            assert private.operation_id == f"run:{run.id}:execute"
            assert private.enqueue_state == "enqueue_pending"
            assert private.execution_context["identity"]["auth_method"] == "api-key"
            assert [item.run_id for item in pending] == [run.id]
            queued = await uow.runs.mark_queued(
                run_id=run.id,
                operation_id=private.operation_id,
                message_id="1-0",
            )
            claimed = await uow.runs.claim_execution(
                run_id=run.id,
                operation_id=private.operation_id,
                owner_id="owner-1",
                workflow_id="workflow-1",
            )
            wrong_operation_replay = await uow.runs.claim_execution(
                run_id=run.id,
                operation_id="run:wrong:execute",
                owner_id="owner-1",
                workflow_id="workflow-1",
            )
            competing = await uow.runs.claim_execution(
                run_id=run.id,
                operation_id=private.operation_id,
                owner_id="owner-2",
                workflow_id="workflow-2",
            )
            await uow.commit()
        assert queued.enqueue_state == "queued"
        assert claimed is True
        assert wrong_operation_replay is False
        assert competing is False
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_service_approval_private_state_is_mutually_exclusive(tmp_path: Path) -> None:
    dsn = _dsn(tmp_path / "approval-queue.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-1")
            session = await uow.sessions.create(
                SessionCreate(tenant_id="tenant-1", user_id="user-1", agent_id="agent-1")
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-1",
                    session_id=session.id,
                    agent_id="agent-1",
                    trace_id="trace-approval-private",
                )
            )
            approval = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-1",
                    run_id=run.id,
                    agent_id="agent-1",
                    action="shell.execute",
                    resource="tool:shell",
                    reason="dangerous",
                    trace_id="trace-approval-private",
                )
            )
            state = await uow.approvals.claim_service_resolution(
                approval_id=approval.approval_id,
                run_id=run.id,
                tenant_id="tenant-1",
                reviewer_id="reviewer-1",
                decision="approve",
                request_hash="a" * 64,
                request_id="req-approve-1",
            )
            await uow.approvals.mark_resolution_queued(
                approval_id=approval.approval_id,
                lease_id=state.lease_id,
                operation_id=state.operation_id,
                message_id="2-0",
            )
            claim_fields = {
                "approval_id": approval.approval_id,
                "tenant_id": "tenant-1",
                "run_id": run.id,
                "lease_id": state.lease_id,
                "operation_id": state.operation_id,
                "request_id": state.request_id,
                "message_id": "2-0",
                "workflow_owner_id": "invalid-owner",
                "workflow_id": "invalid-workflow",
            }
            for field, invalid in (
                ("tenant_id", "other-tenant"),
                ("run_id", str(uuid4())),
                ("request_id", "other-request"),
                ("message_id", "other-message"),
            ):
                assert not await uow.approvals.claim_resolution_execution(
                    **{**claim_fields, field: invalid}
                )
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == approval.approval_id)
                .values(resolution_reviewer_id="")
            )
            assert not await uow.approvals.claim_resolution_execution(**claim_fields)
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == approval.approval_id)
                .values(resolution_reviewer_id="reviewer-1", resolution_request_hash="")
            )
            assert not await uow.approvals.claim_resolution_execution(**claim_fields)
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == approval.approval_id)
                .values(resolution_request_hash="a" * 64)
            )
            owned = await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id="tenant-1",
                run_id=run.id,
                lease_id=state.lease_id,
                operation_id=state.operation_id,
                request_id=state.request_id,
                message_id="2-0",
                workflow_owner_id="owner-1",
                workflow_id="workflow-1",
            )
            await uow.commit()

        assert "resolution_enqueue_state" not in approval.to_payload()
        assert owned is True
        async with storage.uow() as uow:
            taken = await uow.approvals.takeover_service_resolution(
                approval_id=approval.approval_id,
                run_id=run.id,
                tenant_id="tenant-1",
                reviewer_id="reviewer-1",
                decision="approve",
                request_hash="a" * 64,
                request_id="req-approve-2",
                expired_before=datetime.now(tz=UTC) + timedelta(seconds=1),
            )
            await uow.commit()
        assert taken is not None
        assert taken.lease_id != state.lease_id
        assert taken.operation_id.endswith(f"lease:{taken.lease_id}")
        assert taken.request_id == "req-approve-2"
        assert taken.enqueue_state == "enqueue_pending"
        assert taken.workflow_id is None
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_matching_service_approve_takes_over_expired_execution_owner(
    tmp_path: Path,
) -> None:
    dsn = _dsn(tmp_path / "approval-takeover.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    identity = IdentityContext(
        tenant_id="tenant-takeover",
        user_id="reviewer-takeover",
        session_id="session-takeover",
        roles=["reviewer"],
        permissions=["*"],
        auth_method="api-key",
    )
    event_bus = EventBus(sink=LocalJsonlEventSink(tmp_path / "takeover-events.jsonl"))
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
    )
    service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=AuditService(storage=storage),
        queue=queue,
        recovery_lease_timeout_seconds=0,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(identity.tenant_id)
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=identity.tenant_id,
                    user_id="submitter",
                    agent_id="agent-1",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=identity.tenant_id,
                    session_id=session.id,
                    agent_id="agent-1",
                    trace_id="trace-approval-recovery",
                )
            )
            approval = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id=identity.tenant_id,
                    run_id=run.id,
                    agent_id="agent-1",
                    action="shell.execute",
                    resource="tool:shell",
                    reason="dangerous",
                    trace_id="trace-approval-recovery",
                )
            )
            await uow.commit()
        await service.approve(
            actor=identity,
            run_id=run.id,
            approval_id=approval.approval_id,
            request_id="request-first",
            comment="same",
        )
        retried = await service.approve(
            actor=identity,
            run_id=run.id,
            approval_id=approval.approval_id,
            request_id="request-claimed-retry",
            comment="same",
        )
        assert retried.approval.status == "waiting"
        with pytest.raises(ApprovalStateConflict):
            await service.approve(
                actor=identity,
                run_id=run.id,
                approval_id=approval.approval_id,
                request_id="request-different-fingerprint",
                comment="different",
            )
        async with storage.uow() as uow:
            first = await uow.approvals.get_resolution_queue_state(approval.approval_id)
            assert first is not None
            assert first.request_id == "request-first"
            assert await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id=identity.tenant_id,
                run_id=run.id,
                lease_id=first.lease_id,
                operation_id=first.operation_id,
                request_id=first.request_id,
                message_id=first.message_id or "",
                workflow_owner_id="dead-owner",
                workflow_id="dead-workflow",
            )
            await uow.commit()
        with pytest.raises(ApprovalStateConflict):
            await service.approve(
                actor=identity,
                run_id=run.id,
                approval_id=approval.approval_id,
                request_id="request-owned-different-fingerprint",
                comment="different while owned",
            )
        async with storage.uow() as uow:
            still_owned = await uow.approvals.get_resolution_queue_state(approval.approval_id)
        assert still_owned is not None
        assert still_owned.lease_id == first.lease_id
        assert still_owned.operation_id == first.operation_id
        assert still_owned.workflow_id == "dead-workflow"
        await service.approve(
            actor=identity,
            run_id=run.id,
            approval_id=approval.approval_id,
            request_id="request-takeover",
            comment="same",
        )
        async with storage.uow() as uow:
            current = await uow.approvals.get_resolution_queue_state(approval.approval_id)
            audits = await uow.audit_logs.list_for_tenant(identity.tenant_id)
        assert current is not None
        assert current.lease_id != first.lease_id
        assert current.request_id == "request-takeover"
        takeover = [row for row in audits if row.action == "approval.resolution_taken_over"]
        assert len(takeover) == 1
        assert takeover[0].payload["evidence"]["old_workflow_id"] == "dead-workflow"
        assert takeover[0].payload["evidence"]["new_lease_id"] == current.lease_id
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_service_approve_only_queues_and_keeps_public_waiting(tmp_path: Path) -> None:
    dsn = _dsn(tmp_path / "service-approve.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    actor = IdentityContext.local_default()
    bus = EventBus(sink=LocalJsonlEventSink(tmp_path / "approval-events.jsonl"))
    orchestrator = RunOrchestrator(storage=storage, event_bus=bus)
    approvals = ApprovalService(
        storage=storage,
        event_bus=bus,
        orchestrator=orchestrator,
        queue=queue,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(actor.tenant_id)
            session = await uow.sessions.create(
                SessionCreate(
                    session_id=actor.session_id,
                    tenant_id=actor.tenant_id,
                    user_id=actor.user_id,
                    agent_id="agent-approval",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=actor.tenant_id,
                    session_id=session.id,
                    agent_id="agent-approval",
                    trace_id="trace-approval-service",
                )
            )
            approval = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id=actor.tenant_id,
                    run_id=run.id,
                    agent_id="agent-approval",
                    action="shell.execute",
                    resource="tool:shell",
                    reason="dangerous",
                    trace_id="trace-approval-service",
                )
            )
            await uow.commit()

        result = await approvals.approve(
            actor=actor,
            run_id=run.id,
            approval_id=approval.approval_id,
            request_id="req-approve",
        )
        delivery = await queue.pickup(consumer_id="worker")
    finally:
        await storage.dispose()

    assert result.approval.status == "waiting"
    assert delivery is not None
    assert delivery.message.kind == "resume_approval"
    assert delivery.message.approval_id == approval.approval_id
