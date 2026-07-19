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
    """验证 API 已入队但 queued 事件确认丢失时，worker 会先补齐证据再执行 run。"""

    class FailQueuedOnceSink:
        """仅让第一条 queued 事件写入失败的本地 sink 包装器。"""

        def __init__(self, path: Path) -> None:
            """创建真实 JSONL delegate，并初始化一次性故障开关。"""

            self._delegate = LocalJsonlEventSink(path)
            self._failed = False

        def bind_run_trace_resolver(self, resolver: Any) -> None:
            """透传 trace resolver 绑定，使包装器保持 EventBus 所需的 sink 行为。"""

            self._delegate.bind_run_trace_resolver(resolver)

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            """对首条 queued 事件注入写入失败，其余事件委托真实本地 sink。"""

            if not self._failed and event.event_type == CanonicalEventType.RUN_QUEUED:
                self._failed = True
                raise OSError("queued evidence unavailable")
            return await self._delegate.write(event)

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            """透传按 cursor 读取，供断言恢复后的事件顺序。"""

            return await self._delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            """透传最后序号查询，保持 sink 协议完整。"""

            return await self._delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            """透传终结状态查询，保持执行器的终结防护可用。"""

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
    """验证 DBOS 已确定失败时，worker 先将 run 收敛为终结状态再确认队列消息。"""

    class FailingOrchestrator:
        """记录 queued 对账与失败收敛调用顺序的编排器替身。"""

        def __init__(self) -> None:
            """初始化调用顺序记录。"""

            self.calls: list[str] = []

        async def reconcile_queued_run(self, **_kwargs: object) -> None:
            """记录 queued 证据对账已执行。"""

            self.calls.append("reconciled")

        async def fail_queued_run(self, **_kwargs: object) -> RunResult:
            """记录失败收敛并返回已终结 run，模拟确定性 DBOS 失败处理。"""

            self.calls.append("failed")
            return RunResult(run_id="run-failed", status=RunStatus.FAILED)

    class DeterministicDBOS:
        """始终返回已耐久确定性失败的 DBOS 替身。"""

        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            """不抛出未知异常，直接返回可安全映射为失败 run 的结果。"""

            return DBOSOperationOutcome(
                status="deterministic_failed",
                error_code="dbos.error",
            )

    class DelegationService:
        """记录 child 委派收敛调用的最小服务替身。"""

        async def reconcile_child_if_delegated(self, run_id: str) -> bool:
            """把委派对账追加到外层调用记录，证明它发生在失败收敛之后。"""

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
        """只记录 queued 对账的编排器替身，确保测试不会触发真实执行。"""

        async def reconcile_queued_run(self, **_kwargs: object) -> None:
            """记录 worker 在确认 delivery 前仍完成 queued 证据对账。"""

            calls.append("reconciled")

    class DBOS:
        """若被调用会留下标记的 DBOS 替身，用于断言 needs-review 路径禁止重放。"""

        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            """记录意外 DBOS 调用并返回成功，以便断言准确暴露错误控制流。"""

            calls.append("dbos")
            return DBOSOperationOutcome(status="succeeded", result={"status": "completed"})

    async def requires_review(_components: object, _message: object) -> bool:
        """强制共享预算恢复检查要求人工复核，模拟副作用结果未知窗口。"""

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
    """验证终结事件在写前或写后确认丢失时，worker 能补齐唯一终结事件再确认 delivery。"""

    class TerminalExecutor(FakeContractExecutor):
        """按参数返回成功或确定性失败的执行器，用于覆盖两种终结状态。"""

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """失败参数为真时直接生成失败结果，否则沿用可终结的基类执行逻辑。"""

            if fails:
                return AgentExecutionResult.failed("deterministic executor failure")
            return await super().run(request, context)

    class FailTerminalOnceSink:
        """在终结事件持久化前或后仅注入一次故障的 PostgreSQL sink 包装器。"""

        def __init__(self, storage: SQLAlchemyStorage) -> None:
            """创建真实 PostgreSQL sink delegate，并初始化一次性故障开关。"""

            self._delegate = PostgreSQLEventSink(storage)
            self._failed = False

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            """按故障窗口模拟写入失败或确认丢失，其余行为交给真实 sink。"""

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
            """透传事件读取，供断言最终只存在一个终结事件。"""

            return await self._delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            """透传最后序号读取，保持 EventBus 所需查询能力。"""

            return await self._delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            """透传终结检查，保持恢复过程使用真实持久化状态。"""

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
    """验证常驻 worker 只初始化一份运行时，并在取消时恰好关闭一次 adapter 与组件。"""

    consumed_twice = asyncio.Event()
    consume_calls = 0

    class Components:
        """记录服务组件关闭次数的最小运行时组件替身。"""

        queue = object()
        storage = SimpleNamespace(dsn="postgresql+asyncpg://unused")
        orchestrator = object()
        approval_service = object()

        def __init__(self) -> None:
            """初始化关闭计数，用于确认取消路径不会重复释放组件。"""

            self.closed = 0

        async def close(self) -> None:
            """记录组件关闭操作，不执行外部资源释放。"""

            self.closed += 1

    class Adapter:
        """记录 DBOS adapter 生命周期的替身，并暴露最新实例供外层断言。"""

        instance: Adapter | None = None

        def __init__(self, **_kwargs: object) -> None:
            """初始化启动/关闭计数并登记自身为最新实例。"""

            self.started = 0
            self.closed = 0
            Adapter.instance = self

        async def start(self) -> None:
            """记录一次 adapter 启动，模拟常驻 worker 的单次初始化。"""

            self.started += 1

        async def close(self) -> None:
            """记录一次 adapter 关闭，模拟取消时的资源释放。"""

            self.closed += 1

    components = Components()

    async def no_recovery(_components: object) -> None:
        """替换启动恢复任务，使本测试只观察运行时生命周期而不引入业务副作用。"""

        return None

    async def consume(*_args: object, **_kwargs: object) -> None:
        """模拟两轮消费后发出信号，证明主循环复用同一已启动运行时。"""

        nonlocal consume_calls
        consume_calls += 1
        if consume_calls >= 2:
            consumed_twice.set()
        await asyncio.sleep(0)

    def build_components(**_kwargs: object) -> Components:
        """替换组件工厂，始终返回同一可观察实例。"""

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
