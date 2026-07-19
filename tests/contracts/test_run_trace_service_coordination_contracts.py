"""Canonical run trace 的 service 提交协调与基础冲突合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

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
    RunTraceConflict,
    RunTraceIdempotencyConflict,
    RunTraceValidationError,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.api.routes.runs import RunCreateRequest, create_run_with_orchestrator


@pytest.mark.asyncio
async def test_service_run_adapter_skips_concurrent_replay_guardrail(tmp_path: Path) -> None:
    """真实 service route helper 必须把 prepare/guardrail/start 放进同一协调窗口。"""

    dsn = sqlite_dsn(tmp_path / "service-submission-lock.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    entered = asyncio.Event()
    release = asyncio.Event()

    class CountingExecutor(FakeContractExecutor):
        """记录真实执行次数的 executor 桩，用于验证并发提交不会重复运行。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """递增调用计数后沿用基础桩结果，保留真实 runtime 调用路径。"""

            self.calls += 1
            return await super().run(request, context)

    class AllowDecision:
        """最小允许决策，避免测试依赖具体 guardrail provider。"""

        decision = "allow"
        reason = "allowed"

        def to_payload(self) -> dict[str, str]:
            """输出事件写入所需的最小可序列化决策形状。"""

            return {"decision": self.decision}

    class CountingGuardrail:
        """阻塞首次检查并记录次数，使并发提交窗口可被确定性观测。"""

        calls = 0

        async def check(self, **_kwargs: object) -> AllowDecision:
            """通知首个提交已进入 guardrail 后等待释放，模拟慢速安全检查。"""

            self.calls += 1
            entered.set()
            await release.wait()
            return AllowDecision()

    executor = CountingExecutor()
    guardrail = CountingGuardrail()
    event_bus = persisted_event_bus(storage, sink)
    orchestrators = tuple(
        RunOrchestrator(
            storage=storage,
            event_bus=event_bus,
            executor_resolver=lambda _agent_id: executor,
        )
        for _ in range(2)
    )
    request = RunCreateRequest(
        agent_id="fake-agent",
        input={"source": "api"},
        idempotency_key="service-key",
    )

    try:
        winner = asyncio.create_task(
            create_run_with_orchestrator(
                request,
                orchestrator=orchestrators[0],
                identity=IdentityContext.local_default(),
                input_guardrail=cast(Any, guardrail),
                request_id="request-winner",
            )
        )
        await entered.wait()
        loser = asyncio.create_task(
            create_run_with_orchestrator(
                request,
                orchestrator=orchestrators[1],
                identity=IdentityContext.local_default(),
                input_guardrail=cast(Any, guardrail),
                request_id="request-loser",
            )
        )
        await asyncio.sleep(0.05)
        assert not loser.done()
        assert guardrail.calls == 1
        release.set()
        first, second = await asyncio.gather(winner, loser)
    finally:
        await storage.dispose()

    assert first.run_id == second.run_id
    assert guardrail.calls == 1
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_service_run_adapter_serializes_same_trace_across_distinct_keys(
    tmp_path: Path,
) -> None:
    """不同幂等键竞争同一 trace 时，失败方不得进入 guardrail 或 executor。"""

    dsn = sqlite_dsn(tmp_path / "service-trace-lock.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    entered = asyncio.Event()
    release = asyncio.Event()

    class CountingExecutor(FakeContractExecutor):
        """记录执行次数的第二组独立 executor 桩，隔离跨测试状态。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """记录调用后复用基础成功行为，用于断言同 trace 不会双执行。"""

            self.calls += 1
            return await super().run(request, context)

    class AllowDecision:
        """为第二个并发场景提供固定 allow 决策。"""

        decision = "allow"
        reason = "allowed"

        def to_payload(self) -> dict[str, str]:
            """提供 guardrail event 需要的稳定最小 payload。"""

            return {"decision": self.decision}

    class CountingGuardrail:
        """控制第二个提交场景的 guardrail 阻塞点并记录进入次数。"""

        calls = 0

        async def check(self, **_kwargs: object) -> AllowDecision:
            """等待测试显式释放，确保竞争者到达同一 trace 的协调锁边界。"""

            self.calls += 1
            entered.set()
            await release.wait()
            return AllowDecision()

    executor = CountingExecutor()
    guardrail = CountingGuardrail()
    event_bus = persisted_event_bus(storage, sink)
    orchestrators = tuple(
        RunOrchestrator(
            storage=storage,
            event_bus=event_bus,
            executor_resolver=lambda _agent_id: executor,
        )
        for _ in range(2)
    )
    identity = IdentityContext.local_default(session_id="same-trace-session")

    async def submit(orchestrator: RunOrchestrator, idempotency_key: str) -> Any:
        """以指定幂等键提交同一显式 trace，供并发冲突场景复用。"""

        return await create_run_with_orchestrator(
            RunCreateRequest(
                agent_id="fake-agent",
                input={"source": idempotency_key},
                idempotency_key=idempotency_key,
            ),
            orchestrator=orchestrator,
            identity=identity,
            input_guardrail=cast(Any, guardrail),
            request_id=idempotency_key,
            trace_id="shared-concurrent-trace",
        )

    try:
        winner = asyncio.create_task(submit(orchestrators[0], "trace-key-one"))
        await entered.wait()
        loser = asyncio.create_task(submit(orchestrators[1], "trace-key-two"))
        await asyncio.sleep(0.05)
        assert not loser.done()
        assert guardrail.calls == 1
        release.set()
        outcomes = await asyncio.gather(winner, loser, return_exceptions=True)
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant(identity.tenant_id)
    finally:
        await storage.dispose()

    assert len([item for item in outcomes if not isinstance(item, BaseException)]) == 1
    assert len([item for item in outcomes if isinstance(item, RunTraceConflict)]) == 1
    assert len(runs) == 1
    assert guardrail.calls == 1
    assert executor.calls == 1
    events = await sink.read(run_id=runs[0].id)
    assert len(events) == 3
    assert {event.request_id for event in events} == {"trace-key-one"}


@pytest.mark.asyncio
async def test_local_runs_bind_generated_and_explicit_trace_before_events(tmp_path: Path) -> None:
    """local runtime 的 persisted context 与全部 run event 使用同一 trace。"""

    dsn = sqlite_dsn(tmp_path / "trace-local.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    try:
        generated_run = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"source_ref": "source://generated"},
            idempotency_key="generated",
        )
        explicit_run = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"source_ref": "source://explicit"},
            idempotency_key="explicit",
            trace_id="Trace-Explicit",
        )
        async with storage.uow() as uow:
            generated_trace = await uow.runs.get_trace(generated_run.run_id)
            explicit_trace = await uow.runs.get_trace(explicit_run.run_id)
    finally:
        await storage.dispose()

    generated_events = await sink.read(run_id=generated_run.run_id)
    explicit_events = await sink.read(run_id=explicit_run.run_id)
    assert generated_trace is not None
    assert generated_trace == generated_trace.lower()
    assert len(generated_trace) == 36
    assert {event.trace_id for event in generated_events} == {generated_trace}
    assert explicit_trace == "Trace-Explicit"
    assert {event.trace_id for event in explicit_events} == {"Trace-Explicit"}


@pytest.mark.asyncio
async def test_trace_conflicts_have_zero_new_run_or_event_side_effects(tmp_path: Path) -> None:
    """全局和 idempotency 冲突都不能新建 run、event 或改写首次绑定。"""

    dsn = sqlite_dsn(tmp_path / "trace-conflict.db")
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    try:
        first = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key="idem-one",
            trace_id="trace-one",
        )
        replay = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key="idem-one",
        )
        same = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key="idem-one",
            trace_id="trace-one",
        )
        with pytest.raises(RunTraceIdempotencyConflict):
            await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                idempotency_key="idem-one",
                trace_id="trace-other",
            )
        with pytest.raises(RunTraceConflict):
            await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                idempotency_key="idem-two",
                trace_id="trace-one",
            )
        with pytest.raises(RunTraceValidationError):
            await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                idempotency_key="idem-three",
                trace_id=" invalid",
            )
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant("default")
    finally:
        await storage.dispose()

    assert replay.run_id == first.run_id
    assert same.run_id == first.run_id
    assert [run.id for run in runs] == [first.run_id]
    assert len(await sink.read(run_id=first.run_id)) == 2
