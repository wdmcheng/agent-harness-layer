"""Service API 提交、worker 执行与身份 fencing 合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.runtime_contract_helpers import FakeContractExecutor, sqlite_dsn

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    InMemoryRunQueue,
    InvalidRunTransition,
    RunOrchestrator,
    RunStatus,
    build_execute_message,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate


@pytest.mark.asyncio
async def test_service_submit_and_worker_execute_share_run_and_identity(tmp_path: Path) -> None:
    """service API只排队；worker从持久化 context执行同一 run。"""

    class RecordingExecutor(FakeContractExecutor):
        calls: list[tuple[AgentExecutionRequest, AgentExecutionContext]]

        def __init__(self) -> None:
            self.calls = []

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            self.calls.append((request, context))
            return AgentExecutionResult.completed({"source_ref": request.input["source_ref"]})

    db_path = tmp_path / "service-runtime.db"
    events_path = tmp_path / "service-events.jsonl"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    queue = InMemoryRunQueue()
    executor = RecordingExecutor()
    identity = IdentityContext(
        tenant_id="tenant-service",
        user_id="user-service",
        session_id="session-service",
        roles=["operator"],
        permissions=["runs:execute"],
        auth_method="api-key",
    )
    event_bus = EventBus(sink=LocalJsonlEventSink(events_path))
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        queue=queue,
        executor_resolver=lambda _agent_id: executor,
    )
    approval_service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        queue=queue,
    )
    try:
        submitted = await orchestrator.submit_run(
            agent_id="fake-agent",
            input={"source_ref": "source://service", "trust_level": "trusted"},
            idempotency_key="client-key",
            identity=identity,
            request_id="req-service",
            trace_id="trace-service",
        )
        assert submitted.status == RunStatus.CREATED
        assert executor.calls == []

        delivery = await queue.pickup(consumer_id="worker-a")
        assert delivery is not None
        completed = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner-service",
            workflow_id="workflow-service",
        )
        replay = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner-service",
            workflow_id="workflow-service",
        )
        waiting_submit = await orchestrator.submit_run(
            agent_id="fake-agent",
            input={"source_ref": "source://guardrail", "trust_level": "untrusted"},
            checkpoint_state={
                "reason": "guardrail approval",
                "policy": {"decision": "require_approval"},
            },
            identity=identity,
            request_id="req-guardrail",
            trace_id="trace-guardrail",
        )
        waiting_delivery = await queue.pickup(consumer_id="worker-a")
        assert waiting_delivery is not None
        waiting = await orchestrator.execute_run(
            run_id=waiting_delivery.message.run_id,
            tenant_id=waiting_delivery.message.tenant_id,
            operation_id=waiting_delivery.message.operation_id,
            owner_id="owner-waiting",
            workflow_id="workflow-waiting",
        )
        async with storage.uow() as uow:
            guardrail_approvals = await uow.approvals.list_by_run(waiting_submit.run_id)
        reviewer = IdentityContext(
            tenant_id=identity.tenant_id,
            user_id="guardrail-reviewer",
            session_id="guardrail-review-session",
            roles=["reviewer"],
            permissions=["*"],
            auth_method="api-key",
        )
        guardrail_approval = guardrail_approvals[0]
        await approval_service.approve(
            actor=reviewer,
            run_id=waiting_submit.run_id,
            approval_id=guardrail_approval.approval_id,
            request_id="req-guardrail-approve",
            comment="approved guardrail",
        )
        approval_delivery = await queue.pickup(consumer_id="worker-approval")
        assert approval_delivery is not None
        assert approval_delivery.message.kind == "resume_approval"
        async with storage.uow() as uow:
            assert await uow.approvals.claim_resolution_execution(
                approval_id=guardrail_approval.approval_id,
                tenant_id=approval_delivery.message.tenant_id,
                run_id=approval_delivery.message.run_id,
                lease_id=approval_delivery.message.resolution_lease_id or "",
                operation_id=approval_delivery.message.operation_id,
                request_id=approval_delivery.message.request_id,
                message_id=approval_delivery.receipt.message_id,
                workflow_owner_id="guardrail-approval-owner",
                workflow_id="guardrail-approval-workflow",
            )
            await uow.commit()
        guardrail_resolved = await approval_service.execute_queued_approval(
            approval_id=guardrail_approval.approval_id,
            tenant_id=approval_delivery.message.tenant_id,
            run_id=approval_delivery.message.run_id,
            operation_id=approval_delivery.message.operation_id,
            lease_id=approval_delivery.message.resolution_lease_id or "",
        )
        events = await LocalJsonlEventSink(events_path).read(run_id=submitted.run_id)
        guardrail_events = await LocalJsonlEventSink(events_path).read(run_id=waiting_submit.run_id)
    finally:
        await storage.dispose()

    assert completed.status == RunStatus.COMPLETED
    assert replay.status == RunStatus.COMPLETED
    assert waiting_submit.status == RunStatus.CREATED
    assert waiting.status == RunStatus.WAITING
    assert len(guardrail_approvals) == 1
    assert guardrail_approvals[0].action == "input.prompt_injection"
    assert guardrail_resolved.run is not None
    assert guardrail_resolved.run.status == RunStatus.COMPLETED
    correlated_types = {
        CanonicalEventType.CHECKPOINT_CREATED,
        CanonicalEventType.RUN_RESUMED,
        CanonicalEventType.RUN_COMPLETED,
    }
    correlated = [event for event in guardrail_events if event.event_type in correlated_types]
    assert len(correlated) == 3
    for event in correlated:
        assert event.request_id == "req-guardrail"
        assert event.trace_id == "trace-guardrail"
        assert event.payload is not None
        assert event.payload["source_ref"] == "source://guardrail"
        assert event.payload["trust_level"] == "untrusted"
    assert len(executor.calls) == 1
    request, context = executor.calls[0]
    assert request.run_id == submitted.run_id
    assert request.input["trust_level"] == "trusted"
    assert context.identity == identity
    assert context.request_id == "req-service"
    assert context.trace_id == "trace-service"
    assert [event.event_type.value for event in events] == [
        "run.queued",
        "run.started",
        "run.completed",
    ]
    assert events[-1].request_id == "req-service"
    assert events[-1].trace_id == "trace-service"
    assert events[-1].payload is not None
    assert events[-1].payload["source_ref"] == "source://service"
    assert events[-1].payload["trust_level"] == "trusted"


@pytest.mark.parametrize("entrypoint", ["reconcile", "execute", "terminal_evidence"])
@pytest.mark.asyncio
async def test_service_worker_rejects_forged_execution_identity_before_side_effects(
    tmp_path: Path, entrypoint: str
) -> None:
    """身份快照 tenant 被篡改时，reconcile 与 execute 都必须在副作用前失败。"""

    class RecordingExecutor(FakeContractExecutor):
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            del request, context
            self.calls += 1
            return AgentExecutionResult.completed({"unexpected": True})

    dsn = sqlite_dsn(tmp_path / f"forged-identity-{entrypoint}.db")
    events_path = tmp_path / f"forged-identity-{entrypoint}.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    executor = RecordingExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
        executor_resolver=lambda _agent_id: executor,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-real")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id="tenant-real",
                    user_id="submitter",
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create_queued(
                RunCreate(
                    tenant_id="tenant-real",
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key="forged-key",
                ),
                execution_context={
                    "identity": {
                        "tenant_id": "tenant-forged",
                        "user_id": "forged-user",
                        "session_id": "forged-session",
                        "roles": [],
                        "permissions": [],
                        "auth_method": "api-key",
                    },
                    "request_id": "request-forged",
                },
                operation_id="run:pending:execute",
                request_id="request-forged",
                effective_idempotency_key="forged-key",
            )
            private = await uow.runs.get_execution(run.id)
            assert private is not None
            if entrypoint in {"execute", "terminal_evidence"}:
                await uow.runs.mark_queued(
                    run_id=run.id,
                    operation_id=private.operation_id,
                    message_id="forged-message",
                )
            if entrypoint == "terminal_evidence":
                await uow.runs.set_status(
                    run.id,
                    RunStatus.FAILED.value,
                    error={"message": "persisted before evidence"},
                )
            await uow.commit()
        message = build_execute_message(
            request_id="request-forged",
            tenant_id="tenant-real",
            run_id=run.id,
            idempotency_key="forged-key",
        )

        with pytest.raises(InvalidRunTransition, match="tenant mismatch"):
            if entrypoint == "reconcile":
                await orchestrator.reconcile_queued_run(
                    message=message,
                    message_id="forged-message",
                )
            elif entrypoint == "execute":
                await orchestrator.execute_run(
                    run_id=run.id,
                    tenant_id="tenant-real",
                    operation_id=message.operation_id,
                    owner_id="forged-owner",
                    workflow_id="forged-workflow",
                )
            else:
                await orchestrator.fail_queued_run(
                    run_id=run.id,
                    tenant_id="tenant-real",
                    reason="dbos.error",
                )

        async with storage.uow() as uow:
            persisted = await uow.runs.get(run.id)
            persisted_private = await uow.runs.get_execution(run.id)
        events = await LocalJsonlEventSink(events_path).read(run_id=run.id)
    finally:
        await storage.dispose()

    assert persisted is not None
    assert persisted_private is not None
    expected_status = RunStatus.FAILED if entrypoint == "terminal_evidence" else RunStatus.CREATED
    assert persisted.status == expected_status
    assert persisted_private.owner_id is None
    expected_enqueue_state = "enqueue_pending" if entrypoint == "reconcile" else "queued"
    assert persisted_private.enqueue_state == expected_enqueue_state
    assert executor.calls == 0
    assert events == []


@pytest.mark.asyncio
async def test_approval_worker_rejects_forged_run_identity_tenant() -> None:
    """approval continuation 必须把 run 身份快照与 resolution 权威 tenant 对账。"""

    state = SimpleNamespace(
        tenant_id="tenant-real",
        run_id="run-1",
        operation_id="operation-1",
        lease_id="lease-1",
        resolution_state="execution_owned",
        reviewer_id="reviewer-1",
    )
    record = SimpleNamespace(tenant_id="tenant-real", run_id="run-1")
    run = SimpleNamespace(tenant_id="tenant-real")
    run_state = SimpleNamespace(
        tenant_id="tenant-real",
        execution_context={
            "identity": {
                "tenant_id": "tenant-forged",
                "user_id": "submitter",
                "session_id": "forged-session",
                "roles": [],
                "permissions": [],
                "auth_method": "api-key",
            }
        },
    )

    class ApprovalRepository:
        async def get_resolution_queue_state(self, _approval_id: str) -> object:
            return state

        async def get(self, _approval_id: str) -> object:
            return record

    class RunRepository:
        async def get(self, _run_id: str) -> object:
            return run

        async def get_execution(self, _run_id: str) -> object:
            return run_state

    class UnitOfWork:
        approvals = ApprovalRepository()
        runs = RunRepository()

        async def __aenter__(self) -> UnitOfWork:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Storage:
        def uow(self) -> UnitOfWork:
            return UnitOfWork()

    class Orchestrator:
        def bind_approval_service(self, _service: ApprovalService) -> None:
            return None

    service = ApprovalService(
        storage=cast(Any, Storage()),
        event_bus=cast(Any, SimpleNamespace()),
        orchestrator=cast(Any, Orchestrator()),
    )
    with pytest.raises(ApprovalStateConflict, match="tenant mismatch"):
        await service.execute_queued_approval(
            approval_id="approval-1",
            tenant_id="tenant-real",
            run_id="run-1",
            operation_id="operation-1",
            lease_id="lease-1",
        )
