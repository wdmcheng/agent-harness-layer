"""Canonical run trace 的规范化与 SQLite 并发创建合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.run_trace_contract_helpers import persisted_event_bus, sqlite_dsn
from tests.contracts.runtime_contract_helpers import FakeContractExecutor

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    RunOrchestrator,
    RunTraceIdempotencyConflict,
    RunTraceValidationError,
    normalize_trace_id,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunRepository, SessionCreate


def test_trace_normalizer_preserves_valid_values_and_rejects_invalid() -> None:
    """Normalizer 不 trim 或折叠大小写，缺失时生成标准 UUID。"""

    assert normalize_trace_id("Trace.A:1-2") == "Trace.A:1-2"
    generated = normalize_trace_id(None)
    assert generated == generated.lower()
    assert len(generated) == 36
    for invalid in ("", " trace", "trace ", "trace/1", "x" * 129, "中"):
        with pytest.raises(RunTraceValidationError):
            normalize_trace_id(invalid)


@pytest.mark.asyncio
async def test_concurrent_missing_caller_trace_atomically_reuses_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个内部候选同时竞争时，loser 不把生成值误判为显式 caller trace。"""

    dsn = sqlite_dsn(tmp_path / "trace-race.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    identity = IdentityContext.local_default()
    async with storage.uow() as uow:
        await uow.tenants.ensure(identity.tenant_id)
        await uow.sessions.ensure(
            SessionCreate(
                session_id=identity.session_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                agent_id="fake-agent",
            )
        )
        await uow.commit()
    barrier = asyncio.Barrier(2)
    original_validate = RunRepository._validate_trace_claim  # pyright: ignore[reportPrivateUsage]
    validated_tasks: set[asyncio.Task[Any]] = set()

    async def synchronized_validate(repository: RunRepository, data: Any) -> None:
        """让两个候选在 trace 校验点汇合，确定性放大首次内部 trace 竞争窗口。"""

        task = asyncio.current_task()
        assert task is not None
        if task not in validated_tasks:
            validated_tasks.add(task)
            await barrier.wait()
        await original_validate(repository, data)

    monkeypatch.setattr(RunRepository, "_validate_trace_claim", synchronized_validate)

    class CountingExecutor(FakeContractExecutor):
        """记录真实 executor 调用次数，验证竞争 loser 不会产生第二次运行副作用。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """递增计数后复用基础执行结果，保持正常 runtime 事件链路。"""

            self.calls += 1
            return await super().run(request, context)

    executor = CountingExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: executor,
    )
    try:
        prepared = await asyncio.gather(
            *(
                orchestrator.prepare_trace(
                    agent_id="fake-agent",
                    idempotency_key="same-key",
                )
                for _ in range(2)
            )
        )
        assert prepared[0] != prepared[1]
        first, second = await asyncio.gather(
            *(
                orchestrator.start_run(
                    agent_id="fake-agent",
                    input={},
                    idempotency_key="same-key",
                    trace_id=candidate,
                )
                for candidate in prepared
            )
        )
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant("default")
        canonical = runs[0].trace_id
        replay = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key="same-key",
            trace_id=canonical,
        )
        with pytest.raises(RunTraceIdempotencyConflict):
            await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                idempotency_key="same-key",
                trace_id="explicitly-different",
            )
    finally:
        await storage.dispose()

    assert first.run_id == second.run_id == replay.run_id == runs[0].id
    assert len(runs) == 1
    assert executor.calls == 1
    assert len(await sink.read(run_id=runs[0].id)) == 2


@pytest.mark.asyncio
async def test_same_explicit_trace_race_replays_run_after_initial_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次查询后才观察到同 key/trace 的提交时，内部 seam 仍重放首次 run。"""

    dsn = sqlite_dsn(tmp_path / "same-trace-race.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "same-trace-events.jsonl")
    identity = IdentityContext.local_default()
    async with storage.uow() as uow:
        await uow.tenants.ensure(identity.tenant_id)
        await uow.sessions.ensure(
            SessionCreate(
                session_id=identity.session_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                agent_id="fake-agent",
            )
        )
        await uow.commit()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_validate = RunRepository._validate_trace_claim  # pyright: ignore[reportPrivateUsage]

    async def pause_later_validation(repository: RunRepository, data: Any) -> None:
        """只暂停后到任务的校验，模拟首次查询后才发生的同 key/trace 提交竞争。"""

        task = asyncio.current_task()
        if task is not None and task.get_name() == "same-trace-later":
            entered.set()
            await release.wait()
        await original_validate(repository, data)

    monkeypatch.setattr(RunRepository, "_validate_trace_claim", pause_later_validation)

    class CountingExecutor(FakeContractExecutor):
        """记录第二个竞态场景的执行次数，避免跨用例共享可变计数。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """记录调用并返回基础桩结果，证明晚到任务只重放而不执行。"""

            self.calls += 1
            return await super().run(request, context)

    executor = CountingExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: executor,
    )
    trace_id = "same-explicit-trace"
    later = asyncio.create_task(
        orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key="same-explicit-key",
            trace_id=trace_id,
        ),
        name="same-trace-later",
    )
    try:
        await entered.wait()
        first = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key="same-explicit-key",
            trace_id=trace_id,
        )
        release.set()
        replay = await later
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant("default")
    finally:
        release.set()
        if not later.done():
            later.cancel()
        await storage.dispose()

    assert first.run_id == replay.run_id == runs[0].id
    assert len(runs) == 1
    assert executor.calls == 1
    assert len(await sink.read(run_id=first.run_id)) == 2


@pytest.mark.asyncio
async def test_submission_coordination_skips_loser_guardrail_across_orchestrators(
    tmp_path: Path,
) -> None:
    """composition 在共享锁内按 replay 标志跳过 loser 的前置副作用。"""

    dsn = sqlite_dsn(tmp_path / "submission-lock.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    guardrail_calls = 0

    class CountingExecutor(FakeContractExecutor):
        """记录跨 orchestrator 提交的真实执行次数，验证共享协调锁的效果。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """累加调用次数后委托基础行为，使测试只观察协调而不伪造执行成功。"""

            self.calls += 1
            return await super().run(request, context)

    executor = CountingExecutor()
    event_bus = persisted_event_bus(storage, sink)
    orchestrators = tuple(
        RunOrchestrator(
            storage=storage,
            event_bus=event_bus,
            executor_resolver=lambda _agent_id: executor,
        )
        for _ in range(2)
    )

    async def submit(orchestrator: RunOrchestrator, *, pause_winner: bool) -> Any:
        """在真实协调锁中执行 prepare、可控 guardrail 与 start，复用 winner/loser 场景。"""

        nonlocal guardrail_calls
        preflight = await orchestrator.prepare_trace(
            agent_id="fake-agent",
            idempotency_key="coordinated-key",
        )
        async with orchestrator.coordinate_run_submission(
            agent_id="fake-agent",
            idempotency_key="coordinated-key",
            trace_id=preflight,
        ):
            prepared = await orchestrator.prepare_trace(
                agent_id="fake-agent",
                idempotency_key="coordinated-key",
                trace_id=preflight,
            )
            if not prepared.replays_existing:
                guardrail_calls += 1
                if pause_winner:
                    entered.set()
                    await release.wait()
            return await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                idempotency_key="coordinated-key",
                trace_id=prepared,
            )

    try:
        winner = asyncio.create_task(submit(orchestrators[0], pause_winner=True))
        await entered.wait()
        loser = asyncio.create_task(submit(orchestrators[1], pause_winner=False))
        await asyncio.sleep(0.05)
        assert not loser.done()
        assert guardrail_calls == 1
        release.set()
        first, second = await asyncio.gather(winner, loser)
    finally:
        await storage.dispose()

    assert first.run_id == second.run_id
    assert guardrail_calls == 1
    assert executor.calls == 1
