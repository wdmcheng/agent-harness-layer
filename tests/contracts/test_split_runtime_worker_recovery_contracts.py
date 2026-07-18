"""Service worker queue recovery、DBOS 收口与常驻循环合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.runtime_contract_helpers import FakeContractExecutor, sqlite_dsn

from agent_harness.adapters.runtime import DBOSOperationOutcome
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    InMemoryRunQueue,
    RunEnqueueUnavailable,
    RunOrchestrator,
    RunResult,
    RunStatus,
    build_execute_message,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.workers import runtime_worker
from app.workers.runtime_worker import consume_one


@pytest.mark.asyncio
async def test_worker_reconciles_queued_evidence_after_api_ack_loss(tmp_path: Path) -> None:
    class FailQueuedOnceSink:
        def __init__(self, path: Path) -> None:
            self._delegate = LocalJsonlEventSink(path)
            self._failed = False

        def bind_run_trace_resolver(self, resolver: Any) -> None:
            self._delegate.bind_run_trace_resolver(resolver)

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            if not self._failed and event.event_type == CanonicalEventType.RUN_QUEUED:
                self._failed = True
                raise OSError("queued evidence unavailable")
            return await self._delegate.write(event)

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            return await self._delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            return await self._delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            return await self._delegate.has_terminal(run_id)

    dsn = sqlite_dsn(tmp_path / "queue-reconcile.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    sink = FailQueuedOnceSink(tmp_path / "queue-reconcile.jsonl")
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        queue=queue,
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    identity = IdentityContext.local_default()
    try:
        with pytest.raises(RunEnqueueUnavailable):
            await orchestrator.submit_run(
                agent_id="fake-agent",
                input={"prompt": "recover"},
                identity=identity,
                request_id="request-reconcile",
            )
        delivery = await queue.pickup(consumer_id="worker-reconcile")
        assert delivery is not None
        await orchestrator.reconcile_queued_run(
            message=delivery.message,
            message_id=delivery.receipt.message_id,
        )
        result = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner-reconcile",
            workflow_id="workflow-reconcile",
        )
        events = await sink.read(run_id=delivery.message.run_id)
    finally:
        await storage.dispose()

    assert result.status == RunStatus.COMPLETED
    assert [event.event_type.value for event in events] == [
        "run.queued",
        "run.started",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_worker_acks_dbos_deterministic_failure_only_after_run_terminal() -> None:
    class FailingOrchestrator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def reconcile_queued_run(self, **_kwargs: object) -> None:
            self.calls.append("reconciled")

        async def fail_queued_run(self, **_kwargs: object) -> RunResult:
            self.calls.append("failed")
            return RunResult(run_id="run-failed", status=RunStatus.FAILED)

    class DeterministicDBOS:
        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            return DBOSOperationOutcome(
                status="deterministic_failed",
                error_code="dbos.error",
            )

    class DelegationService:
        async def reconcile_child_if_delegated(self, run_id: str) -> bool:
            orchestrator.calls.append(f"delegation:{run_id}")
            return True

    queue = InMemoryRunQueue()
    message = build_execute_message(
        request_id="request-failed",
        tenant_id="tenant-failed",
        run_id="run-failed",
        idempotency_key="key-failed",
    )
    await queue.enqueue(message)
    orchestrator = FailingOrchestrator()
    components = SimpleNamespace(
        queue=queue,
        orchestrator=orchestrator,
        approval_service=None,
        delegation_service=DelegationService(),
    )
    consumed = await consume_one(
        cast(Any, components),
        cast(Any, DeterministicDBOS()),
        consumer_id="worker-failed",
    )

    assert consumed == "run-failed"
    assert orchestrator.calls == ["reconciled", "failed", "delegation:run-failed"]
    assert await queue.pickup(consumer_id="worker-after-ack") is None


@pytest.mark.asyncio
async def test_worker_acks_started_unknown_without_replaying_dbos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shared ledger 已 needs_review 时确认 delivery，保留 run 非 terminal。"""

    calls: list[str] = []

    class Orchestrator:
        async def reconcile_queued_run(self, **_kwargs: object) -> None:
            calls.append("reconciled")

    class DBOS:
        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            calls.append("dbos")
            return DBOSOperationOutcome(status="succeeded", result={"status": "completed"})

    async def requires_review(_components: object, _message: object) -> bool:
        return True

    monkeypatch.setattr(
        runtime_worker,
        "_shared_budget_requires_manual_review",
        requires_review,
    )
    queue = InMemoryRunQueue()
    await queue.enqueue(
        build_execute_message(
            request_id="request-needs-review",
            tenant_id="tenant-a",
            run_id="run-needs-review",
            idempotency_key="key-needs-review",
        )
    )
    components = SimpleNamespace(
        queue=queue,
        orchestrator=Orchestrator(),
        approval_service=None,
        delegation_service=None,
    )

    consumed = await consume_one(
        cast(Any, components),
        cast(Any, DBOS()),
        consumer_id="worker-needs-review",
    )

    assert consumed == "run-needs-review"
    assert calls == ["reconciled"]
    assert await queue.pickup(consumer_id="worker-after-review-ack") is None


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.asyncio
async def test_queued_terminal_state_reconciles_missing_event_before_ack(
    tmp_path: Path, mode: str, fails: bool
) -> None:
    class TerminalExecutor(FakeContractExecutor):
        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            if fails:
                return AgentExecutionResult.failed("deterministic executor failure")
            return await super().run(request, context)

    class FailTerminalOnceSink:
        def __init__(self, storage: SQLAlchemyStorage) -> None:
            self._delegate = PostgreSQLEventSink(storage)
            self._failed = False

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            should_fail = not self._failed and event.terminal
            if should_fail and mode == "before":
                self._failed = True
                raise OSError("terminal evidence unavailable")
            persisted = await self._delegate.write(event)
            if should_fail and mode == "after":
                self._failed = True
                raise OSError("terminal evidence acknowledgement lost")
            return persisted

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            return await self._delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            return await self._delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            return await self._delegate.has_terminal(run_id)

    dsn = sqlite_dsn(tmp_path / f"terminal-reconcile-{mode}-{fails}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    sink = FailTerminalOnceSink(storage)
    identity = IdentityContext.local_default()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        queue=queue,
        executor_resolver=lambda _agent_id: TerminalExecutor(),
    )
    try:
        submitted = await orchestrator.submit_run(
            agent_id="fake-agent",
            input={"prompt": "terminal"},
            identity=identity,
        )
        delivery = await queue.pickup(consumer_id="terminal-worker")
        assert delivery is not None
        await orchestrator.reconcile_queued_run(
            message=delivery.message,
            message_id=delivery.receipt.message_id,
        )
        with pytest.raises(OSError, match="terminal evidence"):
            await orchestrator.execute_run(
                run_id=submitted.run_id,
                tenant_id=identity.tenant_id,
                operation_id=delivery.message.operation_id,
                owner_id="terminal-owner",
                workflow_id="terminal-workflow",
            )
        reconciled = await orchestrator.fail_queued_run(
            run_id=submitted.run_id,
            tenant_id=identity.tenant_id,
            reason="dbos.error",
        )
        events = await sink.read(run_id=submitted.run_id)
    finally:
        await storage.dispose()

    expected_status = RunStatus.FAILED if fails else RunStatus.COMPLETED
    expected_event = CanonicalEventType.RUN_FAILED if fails else CanonicalEventType.RUN_COMPLETED
    assert reconciled.status == expected_status
    assert sum(event.terminal for event in events) == 1
    assert events[-1].event_type == expected_event


@pytest.mark.asyncio
async def test_service_worker_keeps_one_runtime_open_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed_twice = asyncio.Event()
    consume_calls = 0

    class Components:
        queue = object()
        storage = SimpleNamespace(dsn="postgresql+asyncpg://unused")
        orchestrator = object()
        approval_service = object()

        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    class Adapter:
        instance: Adapter | None = None

        def __init__(self, **_kwargs: object) -> None:
            self.started = 0
            self.closed = 0
            Adapter.instance = self

        async def start(self) -> None:
            self.started += 1

        async def close(self) -> None:
            self.closed += 1

    components = Components()

    async def no_recovery(_components: object) -> None:
        return None

    async def consume(*_args: object, **_kwargs: object) -> None:
        nonlocal consume_calls
        consume_calls += 1
        if consume_calls >= 2:
            consumed_twice.set()
        await asyncio.sleep(0)

    def build_components(**_kwargs: object) -> Components:
        return components

    monkeypatch.setattr(runtime_worker, "build_runtime_components", build_components)
    monkeypatch.setattr(runtime_worker, "DBOSServiceRuntimeAdapter", Adapter)
    monkeypatch.setattr(runtime_worker, "_recover_pending_enqueue", no_recovery)
    monkeypatch.setattr(runtime_worker, "_recover_pending_usage", no_recovery)
    monkeypatch.setattr(runtime_worker, "consume_one", consume)
    task = asyncio.create_task(runtime_worker.run_forever())
    await asyncio.wait_for(consumed_twice.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert consume_calls >= 2
    assert Adapter.instance is not None
    assert Adapter.instance.started == 1
    assert Adapter.instance.closed == 1
    assert components.closed == 1
