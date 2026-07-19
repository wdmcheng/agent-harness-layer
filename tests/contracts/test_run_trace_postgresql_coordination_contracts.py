"""Canonical run trace 的 PostgreSQL 跨 engine 协调合同。"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from tests.contracts.runtime_contract_helpers import FakeContractExecutor

from agent_harness.events import EventBus, PostgreSQLEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    RunOrchestrator,
    RunTraceConflict,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunRepository, SessionCreate
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL advisory-lock 合同由 service 环境注入 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_submission_coordination_serializes_independent_storages() -> None:
    """两个独立 engine 必须用数据库锁收敛同 key 的全部前置与运行副作用。"""

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    await asyncio.to_thread(run_migrations, dsn)
    storages = (
        SQLAlchemyStorage.from_dsn(dsn),
        SQLAlchemyStorage.from_dsn(dsn),
    )
    identity = IdentityContext(
        tenant_id=f"trace-lock-{uuid4()}",
        user_id="trace-lock-user",
        session_id=str(uuid4()),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    guardrail_calls = 0

    class CountingExecutor(FakeContractExecutor):
        """记录跨数据库 engine 的执行次数，验证 advisory lock 覆盖真实副作用。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """累加执行计数并复用基础成功结果，保持 service 真实调用路径。"""

            self.calls += 1
            return await super().run(request, context)

    executor = CountingExecutor()
    orchestrators = tuple(
        RunOrchestrator(
            storage=storage,
            event_bus=EventBus(
                sink=PostgreSQLEventSink(storage),
                run_trace_resolver=StorageRunTraceResolver(storage),
            ),
            identity=identity,
            executor_resolver=lambda _agent_id: executor,
        )
        for storage in storages
    )
    idempotency_key = f"postgres-coordinated-{uuid4()}"

    async def submit(orchestrator: RunOrchestrator, *, pause_winner: bool) -> Any:
        """在 PostgreSQL 协调锁内提交一次 run，并可暂停 winner 形成竞争窗口。"""

        nonlocal guardrail_calls
        preflight = await orchestrator.prepare_trace(
            agent_id="fake-agent",
            idempotency_key=idempotency_key,
            identity=identity,
        )
        async with orchestrator.coordinate_run_submission(
            agent_id="fake-agent",
            idempotency_key=idempotency_key,
            trace_id=preflight,
            identity=identity,
        ):
            prepared = await orchestrator.prepare_trace(
                agent_id="fake-agent",
                idempotency_key=idempotency_key,
                identity=identity,
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
                idempotency_key=idempotency_key,
                identity=identity,
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
        async with storages[0].uow() as uow:
            runs = await uow.runs.list_for_tenant(identity.tenant_id)
    finally:
        await asyncio.gather(*(storage.dispose() for storage in storages))

    assert first.run_id == second.run_id == runs[0].id
    assert len(runs) == 1
    assert guardrail_calls == 1
    assert executor.calls == 1


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL 内部幂等窗口合同由 service 环境注入 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_same_explicit_trace_race_replays_after_initial_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立 engine 在首次查询后的同 key/trace 竞争仍只执行一个 run。"""

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    await asyncio.to_thread(run_migrations, dsn)
    storages = (SQLAlchemyStorage.from_dsn(dsn), SQLAlchemyStorage.from_dsn(dsn))
    sinks = (PostgreSQLEventSink(storages[0]), PostgreSQLEventSink(storages[1]))
    identity = IdentityContext(
        tenant_id=f"same-trace-race-{uuid4()}",
        user_id="same-trace-user",
        session_id=str(uuid4()),
    )
    async with storages[0].uow() as uow:
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
        """暂停命名为 later 的任务，模拟数据库首次查询后被竞争提交抢先的窗口。"""

        task = asyncio.current_task()
        if task is not None and task.get_name() == "postgres-same-trace-later":
            entered.set()
            await release.wait()
        await original_validate(repository, data)

    monkeypatch.setattr(RunRepository, "_validate_trace_claim", pause_later_validation)

    class CountingExecutor(FakeContractExecutor):
        """记录相同显式 trace 竞争下的 executor 次数，确保 later 只重放。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """记录调用后沿用基础桩行为，避免测试绕过生产 run 路径。"""

            self.calls += 1
            return await super().run(request, context)

    executor = CountingExecutor()
    orchestrators = tuple(
        RunOrchestrator(
            storage=storage,
            event_bus=EventBus(
                sink=sink,
                run_trace_resolver=StorageRunTraceResolver(storage),
            ),
            identity=identity,
            executor_resolver=lambda _agent_id: executor,
        )
        for storage, sink in zip(storages, sinks, strict=True)
    )
    idempotency_key = f"same-explicit-key-{uuid4()}"
    trace_id = f"same-explicit-trace-{uuid4()}"
    later = asyncio.create_task(
        orchestrators[1].start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key=idempotency_key,
            identity=identity,
            trace_id=trace_id,
        ),
        name="postgres-same-trace-later",
    )
    try:
        await entered.wait()
        first = await orchestrators[0].start_run(
            agent_id="fake-agent",
            input={},
            idempotency_key=idempotency_key,
            identity=identity,
            trace_id=trace_id,
        )
        release.set()
        replay = await later
        async with storages[0].uow() as uow:
            runs = await uow.runs.list_for_tenant(identity.tenant_id)
        events = await sinks[0].read(run_id=first.run_id)
    finally:
        release.set()
        if not later.done():
            later.cancel()
        await asyncio.gather(*(storage.dispose() for storage in storages))

    assert first.run_id == replay.run_id == runs[0].id
    assert len(runs) == 1
    assert executor.calls == 1
    assert len(events) == 2


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL 全局 trace advisory-lock 合同由 service 环境注入 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_trace_coordination_serializes_distinct_keys() -> None:
    """独立 engine 的不同幂等键仍需按全局 trace 串行，并让失败方零副作用。"""

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    await asyncio.to_thread(run_migrations, dsn)
    storages = (
        SQLAlchemyStorage.from_dsn(dsn),
        SQLAlchemyStorage.from_dsn(dsn),
    )
    identity = IdentityContext(
        tenant_id=f"trace-global-lock-{uuid4()}",
        user_id="trace-global-user",
        session_id=str(uuid4()),
    )
    trace_id = f"shared-trace-{uuid4()}"
    entered = asyncio.Event()
    release = asyncio.Event()
    guardrail_calls = 0

    class CountingExecutor(FakeContractExecutor):
        """记录不同幂等键共享 trace 时的执行次数，验证全局 trace 锁。"""

        calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """记录执行并返回基础结果，供断言失败方未进入 executor。"""

            self.calls += 1
            return await super().run(request, context)

    executor = CountingExecutor()
    orchestrators = tuple(
        RunOrchestrator(
            storage=storage,
            event_bus=EventBus(
                sink=PostgreSQLEventSink(storage),
                run_trace_resolver=StorageRunTraceResolver(storage),
            ),
            identity=identity,
            executor_resolver=lambda _agent_id: executor,
        )
        for storage in storages
    )

    async def submit(
        orchestrator: RunOrchestrator,
        *,
        idempotency_key: str,
        pause_winner: bool,
    ) -> Any:
        """以给定幂等键提交共享 trace，并在 winner guardrail 后停顿以观测串行化。"""

        nonlocal guardrail_calls
        preflight = await orchestrator.prepare_trace(
            agent_id="fake-agent",
            idempotency_key=idempotency_key,
            identity=identity,
            trace_id=trace_id,
        )
        async with orchestrator.coordinate_run_submission(
            agent_id="fake-agent",
            idempotency_key=idempotency_key,
            identity=identity,
            trace_id=preflight,
        ):
            prepared = await orchestrator.prepare_trace(
                agent_id="fake-agent",
                idempotency_key=idempotency_key,
                identity=identity,
                trace_id=preflight,
            )
            guardrail_calls += 1
            if pause_winner:
                entered.set()
                await release.wait()
            return await orchestrator.start_run(
                agent_id="fake-agent",
                input={},
                idempotency_key=idempotency_key,
                identity=identity,
                trace_id=prepared,
            )

    try:
        winner = asyncio.create_task(
            submit(
                orchestrators[0],
                idempotency_key="global-trace-key-one",
                pause_winner=True,
            )
        )
        await entered.wait()
        loser = asyncio.create_task(
            submit(
                orchestrators[1],
                idempotency_key="global-trace-key-two",
                pause_winner=False,
            )
        )
        await asyncio.sleep(0.05)
        assert not loser.done()
        assert guardrail_calls == 1
        release.set()
        outcomes = await asyncio.gather(winner, loser, return_exceptions=True)
        async with storages[0].uow() as uow:
            runs = await uow.runs.list_for_tenant(identity.tenant_id)
    finally:
        await asyncio.gather(*(storage.dispose() for storage in storages))

    assert len([item for item in outcomes if not isinstance(item, BaseException)]) == 1
    assert len([item for item in outcomes if isinstance(item, RunTraceConflict)]) == 1
    assert len(runs) == 1
    assert guardrail_calls == 1
    assert executor.calls == 1
