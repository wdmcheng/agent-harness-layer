"""Service 审批执行 owner 接管与异步队列合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_runtime_storage_contracts import (
    ApprovalCreate as ApprovalCreate,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    ApprovalService as ApprovalService,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    ApprovalStateConflict as ApprovalStateConflict,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    AuditService as AuditService,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    InMemoryRunQueue as InMemoryRunQueue,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    Path as Path,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    SessionCreate as SessionCreate,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    _dsn as _dsn,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    pytest as pytest,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    run_migrations as run_migrations,
)


@pytest.mark.asyncio
async def test_matching_service_approve_takes_over_expired_execution_owner(
    tmp_path: Path,
) -> None:
    """验证相同审批请求可接管过期 worker owner，并保留可审计的 lease 交接证据。"""

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
    """验证 service approve 仅入队，公开审批状态保持 waiting 直至 worker 执行。"""

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
