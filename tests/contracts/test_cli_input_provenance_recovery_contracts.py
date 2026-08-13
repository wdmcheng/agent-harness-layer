"""CLI provenance 的 local、queued 与 approval 恢复合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import update
from tests.contracts.runtime_contract_helpers import sqlite_dsn
from tests.contracts.test_approval_execution_contracts import build_approval_flow

from agent_harness.adapters.runtime import DBOSOperation, DBOSOperationOutcome
from agent_harness.approvals import ApprovalService
from agent_harness.audit import AuditService
from agent_harness.delegation import DelegationRequest, delegation_request_hash
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    InMemoryRunQueue,
    RunOrchestrator,
)
from agent_harness.runtime._continuation_context import (
    PROVENANCE_SCHEMA_VERSION,
    RunInputProvenance,
    execution_provenance,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.run_models import AgentRunModel
from app.workers.runtime_worker import consume_one
from app.workers.runtime_worker_operations import execute_approval_operation


class _RecordingExecutor:
    def __init__(self) -> None:
        self.run_calls: list[tuple[AgentExecutionRequest, AgentExecutionContext]] = []
        self.resume_calls: list[AgentExecutionContext] = []

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        self.run_calls.append((request, context))
        return AgentExecutionResult.completed({"ok": True})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del request, grant
        self.resume_calls.append(context)
        return AgentExecutionResult.completed({"ok": True})


@pytest.mark.asyncio
async def test_local_cli_provenance_survives_persistence_and_idempotent_replay(
    tmp_path: Path,
) -> None:
    dsn = sqlite_dsn(tmp_path / "local-provenance.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    executor = _RecordingExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(tmp_path / "events.jsonl")),
        executor_resolver=lambda _agent_id: executor,
    )
    business_input = {"prompt": "hello", "source": "business-value"}
    try:
        first = await cast(Any, orchestrator)._start_run_with_provenance(
            agent_id="fake-agent",
            input=business_input,
            idempotency_key="cli-key",
            trace_id="cli-trace",
            request_id="execution-request",
            provenance=RunInputProvenance(source="cli"),
        )
        replay = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "ignored"},
            idempotency_key="cli-key",
            trace_id="cli-trace",
        )
        async with storage.uow() as uow:
            run = await uow.runs.get(first.run_id)
            record = await uow.runs.get_execution_context_record(first.run_id)
    finally:
        await storage.dispose()

    assert first.run_id == replay.run_id
    assert len(executor.run_calls) == 1
    request, context = executor.run_calls[0]
    assert request.input == business_input
    assert execution_provenance(context) == RunInputProvenance(source="cli")
    assert run is not None and run.input == business_input
    assert record is not None
    assert cast(dict[str, Any], record.execution_context)["input_provenance"] == {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source": "cli",
        "execution_request_id": "execution-request",
    }


@pytest.mark.asyncio
async def test_terminal_recovery_uses_current_entry_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次终态发布中断后，幂等恢复使用本次入口 ID 补写 evidence。"""

    dsn = sqlite_dsn(tmp_path / "terminal-recovery.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "terminal-recovery-events.jsonl")
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        executor_resolver=lambda _agent_id: _RecordingExecutor(),
    )
    original_write = sink.write
    interrupted = False

    async def interrupt_first_terminal(event: Any) -> Any:
        """只在首次 terminal 写入前中断，保留已提交的 run 终态。"""

        nonlocal interrupted
        if event.event_type == CanonicalEventType.RUN_COMPLETED and not interrupted:
            interrupted = True
            raise OSError("terminal evidence interrupted")
        return await original_write(event)

    monkeypatch.setattr(sink, "write", interrupt_first_terminal)
    try:
        with pytest.raises(OSError, match="terminal evidence interrupted"):
            await cast(Any, orchestrator)._start_run_with_provenance(
                agent_id="fake-agent",
                input={"prompt": "recover"},
                idempotency_key="terminal-recovery-key",
                request_id="execution-original",
                provenance=RunInputProvenance(source="cli"),
            )
        monkeypatch.setattr(sink, "write", original_write)
        recovered = await cast(Any, orchestrator)._start_run_with_provenance(
            agent_id="fake-agent",
            input={"prompt": "ignored"},
            idempotency_key="terminal-recovery-key",
            request_id="recovery-current",
            provenance=RunInputProvenance(source="cli"),
        )
        events = await sink.read(run_id=recovered.run_id)
    finally:
        await storage.dispose()

    terminal = [event for event in events if event.terminal]
    assert len(terminal) == 1
    assert terminal[0].request_id == "recovery-current"


@pytest.mark.asyncio
async def test_malformed_queued_context_fails_before_claim_event_or_executor(
    tmp_path: Path,
) -> None:
    dsn = sqlite_dsn(tmp_path / "queued-provenance.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "queued-events.jsonl")
    identity = IdentityContext.local_default()
    executor = _RecordingExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        executor_resolver=lambda _agent_id: executor,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(identity.tenant_id)
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=identity.session_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create_queued(
                RunCreate(
                    tenant_id=identity.tenant_id,
                    session_id=session.id,
                    agent_id="fake-agent",
                    input={"prompt": "must-not-run"},
                    trace_id="queued-trace",
                ),
                execution_context={"provenance": {"source": "cli"}},
                operation_id="run:pending:execute",
                request_id="queue-delivery-id",
                effective_idempotency_key=None,
            )
            private = await uow.runs.get_execution(run.id)
            assert private is not None
            await uow.runs.mark_queued(
                run_id=run.id,
                operation_id=private.operation_id,
                message_id="message-1",
            )
            await uow.commit()

        with pytest.raises(ValueError, match="execution_context.provenance_invalid"):
            await orchestrator.execute_run(
                run_id=run.id,
                tenant_id=identity.tenant_id,
                operation_id=private.operation_id,
                owner_id="must-not-claim",
                workflow_id="must-not-start",
            )
        async with storage.uow() as uow:
            after = await uow.runs.get_execution(run.id)
    finally:
        await storage.dispose()

    assert after is not None and after.owner_id is None and after.workflow_id is None
    assert executor.run_calls == []
    assert await sink.read(run_id=run.id) == []


@pytest.mark.asyncio
async def test_queued_reclaim_rebuilds_typed_provenance_without_borrowing_delivery_id(
    tmp_path: Path,
) -> None:
    dsn = sqlite_dsn(tmp_path / "queued-reclaim.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    executor = _RecordingExecutor()
    identity = IdentityContext.local_default()

    def resolve_executor(_agent_id: str) -> _RecordingExecutor:
        return executor

    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(tmp_path / "reclaim-events.jsonl")),
        executor_resolver=resolve_executor,
        queue=queue,
    )
    try:
        submitted = await cast(Any, orchestrator)._submit_run_with_provenance(
            agent_id="fake-agent",
            input={"prompt": "queued"},
            identity=identity,
            request_id=None,
            provenance=RunInputProvenance(source="cli"),
        )
        first = await queue.pickup(consumer_id="worker-1")
        assert first is not None
        reclaimed = await queue.reclaim(consumer_id="worker-2", min_idle_seconds=0)
        assert reclaimed is not None
        assert reclaimed.message.request_id
        result = await orchestrator.execute_run(
            run_id=submitted.run_id,
            tenant_id=identity.tenant_id,
            operation_id=reclaimed.message.operation_id,
            owner_id="owner-2",
            workflow_id="workflow-2",
        )
        await queue.ack(reclaimed.receipt)
    finally:
        await storage.dispose()

    assert result.status.value == "completed"
    assert len(executor.run_calls) == 1
    _request, context = executor.run_calls[0]
    assert context.request_id is None
    assert execution_provenance(context) == RunInputProvenance(source="cli")


@pytest.mark.asyncio
async def test_service_terminal_recovery_uses_current_delivery_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execution ID 可为空，但 DBOS 失败终态必须使用本次 delivery ID。"""

    from app.workers import runtime_worker

    class DeterministicFailureDBOS:
        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            return DBOSOperationOutcome(
                status="deterministic_failed",
                error_code="dbos.provenance-recovery",
            )

    class DelegationService:
        async def reconcile_child_if_delegated(self, _run_id: str) -> bool:
            return False

    dsn = sqlite_dsn(tmp_path / "service-terminal-recovery.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "service-terminal-recovery.jsonl")
    queue = InMemoryRunQueue()
    identity = IdentityContext.local_default()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        executor_resolver=lambda _agent_id: _RecordingExecutor(),
        queue=queue,
    )
    monkeypatch.setattr(runtime_worker, "RECLAIM_IDLE_SECONDS", 0)
    try:
        submitted = await cast(Any, orchestrator)._submit_run_with_provenance(
            agent_id="fake-agent",
            input={"prompt": "queued failure"},
            identity=identity,
            request_id=None,
            provenance=RunInputProvenance(source="cli"),
        )
        async with storage.uow() as uow:
            execution = await uow.runs.get_execution(submitted.run_id)
            context_record = await uow.runs.get_execution_context_record(submitted.run_id)
        assert execution is not None and execution.request_id
        assert context_record is not None
        assert cast(dict[str, Any], context_record.execution_context)["request_id"] is None

        consumed = await consume_one(
            cast(
                Any,
                SimpleNamespace(
                    queue=queue,
                    orchestrator=orchestrator,
                    storage=None,
                    approval_service=None,
                    delegation_service=DelegationService(),
                ),
            ),
            cast(Any, DeterministicFailureDBOS()),
            consumer_id="terminal-recovery-worker",
        )
        events = await sink.read(run_id=submitted.run_id)
    finally:
        await storage.dispose()

    assert consumed == submitted.run_id
    terminal = [event for event in events if event.terminal]
    assert len(terminal) == 1
    assert terminal[0].request_id == execution.request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_request_id", ["execution-original", None])
async def test_approval_resume_separates_execution_and_resolution_request_ids(
    tmp_path: Path,
    execution_request_id: str | None,
) -> None:
    """executor 读取首次执行 ID；当前 resumed/terminal evidence 使用 resolution ID。"""

    def passthrough(arguments: dict[str, object]) -> dict[str, object]:
        return arguments

    storage, sink, service, orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path, handler=passthrough
    )
    executor = _RecordingExecutor()
    resolution_request_id = "approval-resolution-current"
    try:
        async with storage.uow() as uow:
            record = await uow.runs.get_execution_context_record(waiting.run_id)
            assert record is not None
            payload = cast(dict[str, Any], record.execution_context)
            payload = {
                **payload,
                "request_id": execution_request_id,
                "input_provenance": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "source": "cli",
                    "execution_request_id": execution_request_id,
                },
            }
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == waiting.run_id)
                .values(execution_context_json=payload)
            )
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            await uow.commit()

        def resolve_executor(_agent_id: str) -> _RecordingExecutor:
            return executor

        cast(Any, orchestrator)._executor_resolver = resolve_executor
        resolved = await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
            request_id=resolution_request_id,
        )
        events = await sink.read(run_id=waiting.run_id)
    finally:
        await storage.dispose()

    assert resolved.run is not None and resolved.run.status.value == "completed"
    assert len(executor.resume_calls) == 1
    resume_context = executor.resume_calls[0]
    assert resume_context.request_id == execution_request_id
    assert execution_provenance(resume_context) == RunInputProvenance(source="cli")
    correlated = [
        event for event in events if event.event_type.value in {"run.resumed", "run.completed"}
    ]
    assert [event.event_type.value for event in correlated] == [
        "run.resumed",
        "run.completed",
    ]
    assert {event.request_id for event in correlated} == {resolution_request_id}


@pytest.mark.asyncio
async def test_service_approval_worker_preserves_execution_and_resolution_planes(
    tmp_path: Path,
) -> None:
    """service worker 续跑时 executor 与新 evidence 分别使用两个 ID 平面。"""

    def passthrough(arguments: dict[str, object]) -> dict[str, object]:
        """返回原始工具参数，隔离本合同与业务 handler 行为。"""

        return arguments

    queue = InMemoryRunQueue()
    storage, sink, _local, orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path,
        handler=passthrough,
        queue=queue,
    )
    executor = _RecordingExecutor()
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        queue=queue,
    )
    resolution_request_id = "service-resolution-current"
    try:
        async with storage.uow() as uow:
            record = await uow.runs.get_execution_context_record(waiting.run_id)
            assert record is not None
            payload = {
                **cast(dict[str, Any], record.execution_context),
                "request_id": "service-execution-original",
                "input_provenance": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "source": "cli",
                    "execution_request_id": "service-execution-original",
                },
            }
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == waiting.run_id)
                .values(execution_context_json=payload)
            )
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            await uow.commit()

        def resolve_executor(_agent_id: str) -> _RecordingExecutor:
            """为 service continuation 返回记录双 ID 平面的执行器。"""

            return executor

        cast(Any, orchestrator)._executor_resolver = resolve_executor
        await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
            request_id=resolution_request_id,
        )
        delivery = await queue.pickup(consumer_id="service-provenance-worker")
        assert delivery is not None
        async with storage.uow() as uow:
            assert await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                lease_id=delivery.message.resolution_lease_id or "",
                operation_id=delivery.message.operation_id,
                request_id=delivery.message.request_id,
                message_id=delivery.receipt.message_id,
                workflow_owner_id="service-provenance-worker",
                workflow_id="service-provenance-workflow",
            )
            await uow.commit()

        class _DelegationService:
            async def reconcile_child_if_delegated(self, _run_id: str) -> bool:
                """当前聚焦合同没有 delegated child，稳定返回未命中。"""

                return False

        await execute_approval_operation(
            cast(
                Any,
                SimpleNamespace(
                    orchestrator=orchestrator,
                    approval_service=service,
                    delegation_service=_DelegationService(),
                ),
            ),
            DBOSOperation(
                kind="resume_approval",
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                operation_id=delivery.message.operation_id,
                approval_id=approval.approval_id,
                resolution_lease_id=delivery.message.resolution_lease_id,
            ),
        )
        events = await sink.read(run_id=waiting.run_id)
    finally:
        await storage.dispose()

    assert executor.resume_calls[0].request_id == "service-execution-original"
    correlated = [
        event for event in events if event.event_type.value in {"run.resumed", "run.completed"}
    ]
    assert {event.request_id for event in correlated} == {resolution_request_id}


@pytest.mark.asyncio
async def test_service_approval_recovery_pending_terminal_uses_resolution_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态已提交但证据中断时，worker 补写 terminal 仍使用当前 resolution ID。"""

    def passthrough(arguments: dict[str, object]) -> dict[str, object]:
        """保留真实 approval continuation，同时隔离外部业务副作用。"""

        return arguments

    queue = InMemoryRunQueue()
    storage, sink, _local, orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path,
        handler=passthrough,
        queue=queue,
    )
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        queue=queue,
    )
    execution_request_id = "service-recovery-execution-original"
    resolution_request_id = "service-recovery-resolution-current"
    original_write = sink.write
    try:
        async with storage.uow() as uow:
            record = await uow.runs.get_execution_context_record(waiting.run_id)
            assert record is not None
            payload = {
                **cast(dict[str, Any], record.execution_context),
                "request_id": execution_request_id,
                "input_provenance": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "source": "cli",
                    "execution_request_id": execution_request_id,
                },
            }
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == waiting.run_id)
                .values(execution_context_json=payload)
            )
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            await uow.commit()

        await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
            request_id=resolution_request_id,
        )
        delivery = await queue.pickup(consumer_id="service-recovery-worker")
        assert delivery is not None
        assert delivery.message.resolution_lease_id is not None
        async with storage.uow() as uow:
            assert await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                lease_id=delivery.message.resolution_lease_id,
                operation_id=delivery.message.operation_id,
                request_id=delivery.message.request_id,
                message_id=delivery.receipt.message_id,
                workflow_owner_id="service-recovery-worker",
                workflow_id="service-recovery-workflow",
            )
            await uow.commit()

        interrupted = False

        async def interrupt_terminal(event: Any) -> Any:
            """只在首次 terminal 写入前中断，留下 recovery_pending 补证窗口。"""

            nonlocal interrupted
            if event.event_type == CanonicalEventType.RUN_COMPLETED and not interrupted:
                interrupted = True
                raise OSError("run.completed evidence interrupted")
            return await original_write(event)

        monkeypatch.setattr(sink, "write", interrupt_terminal)
        with pytest.raises(OSError, match="run.completed evidence interrupted"):
            await service.execute_queued_approval(
                approval_id=approval.approval_id,
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                operation_id=delivery.message.operation_id,
                lease_id=delivery.message.resolution_lease_id,
            )
        async with storage.uow() as uow:
            pending = await uow.approvals.get_resolution_queue_state(approval.approval_id)
        assert pending is not None and pending.resolution_state == "recovery_pending"

        monkeypatch.setattr(sink, "write", original_write)
        recovered = await service.finalize_queued_failure(
            approval_id=approval.approval_id,
            tenant_id=delivery.message.tenant_id,
            run_id=delivery.message.run_id,
            operation_id=delivery.message.operation_id,
            lease_id=delivery.message.resolution_lease_id,
            error_code="dbos.recovery-pending-evidence",
        )
        events = await sink.read(run_id=waiting.run_id)
    finally:
        await storage.dispose()

    assert recovered.run is not None and recovered.run.status.value == "completed"
    terminal = [event for event in events if event.terminal]
    assert len(terminal) == 1
    assert terminal[0].request_id == resolution_request_id


@pytest.mark.asyncio
async def test_service_approval_deterministic_failure_uses_resolution_request_id(
    tmp_path: Path,
) -> None:
    """DBOS 确定失败补写的 terminal 仍属于当前 approval resolution。"""

    def passthrough(arguments: dict[str, object]) -> dict[str, object]:
        """保留真实 approval checkpoint，同时避免引入外部副作用。"""

        return arguments

    queue = InMemoryRunQueue()
    storage, sink, _local, orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path,
        handler=passthrough,
        queue=queue,
    )
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        queue=queue,
    )
    execution_request_id = "service-failed-execution-original"
    resolution_request_id = "service-failed-resolution-current"
    try:
        async with storage.uow() as uow:
            record = await uow.runs.get_execution_context_record(waiting.run_id)
            assert record is not None
            payload = {
                **cast(dict[str, Any], record.execution_context),
                "request_id": execution_request_id,
                "input_provenance": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "source": "cli",
                    "execution_request_id": execution_request_id,
                },
            }
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == waiting.run_id)
                .values(execution_context_json=payload)
            )
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            await uow.commit()

        await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
            request_id=resolution_request_id,
        )
        delivery = await queue.pickup(consumer_id="service-failed-provenance-worker")
        assert delivery is not None
        assert delivery.message.resolution_lease_id is not None
        async with storage.uow() as uow:
            assert await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                lease_id=delivery.message.resolution_lease_id,
                operation_id=delivery.message.operation_id,
                request_id=delivery.message.request_id,
                message_id=delivery.receipt.message_id,
                workflow_owner_id="service-failed-provenance-worker",
                workflow_id="service-failed-provenance-workflow",
            )
            await uow.commit()

        await service.finalize_queued_failure(
            approval_id=approval.approval_id,
            tenant_id=delivery.message.tenant_id,
            run_id=delivery.message.run_id,
            operation_id=delivery.message.operation_id,
            lease_id=delivery.message.resolution_lease_id,
            error_code="dbos.provenance-deterministic-failure",
        )
        events = await sink.read(run_id=waiting.run_id)
    finally:
        await storage.dispose()

    terminal = [event for event in events if event.terminal]
    assert len(terminal) == 1
    assert terminal[0].request_id == resolution_request_id


def test_delegation_hash_keeps_business_source_and_rejects_private_provenance() -> None:
    """delegation 只接收既有业务 input；私有 provenance 不能进入其 DTO 或 hash。"""

    identity = IdentityContext.local_default()
    request = DelegationRequest(
        parent_run_id="parent-run",
        source_agent_id="source-agent",
        target_agent_id="target-agent",
        child_input={"prompt": "delegate", "source": "business-value"},
        idempotency_key="delegate-key",
    )
    baseline = delegation_request_hash(request, identity=identity)

    assert request.child_input["source"] == "business-value"
    assert delegation_request_hash(request.model_copy(deep=True), identity=identity) == baseline
    with pytest.raises(ValueError):
        DelegationRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "input_provenance": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "source": "cli",
                    "execution_request_id": None,
                },
            }
        )
