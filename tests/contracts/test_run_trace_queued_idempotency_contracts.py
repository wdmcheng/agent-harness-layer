"""Queued service 入口的 canonical trace 幂等竞争合同。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.run_trace_contract_helpers import persisted_event_bus, sqlite_dsn
from tests.contracts.run_trace_revision_hardening_postgresql_helpers import postgres_database

from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    InMemoryRunQueue,
    RunOrchestrator,
    RunTraceConflict,
    RunTraceIdempotencyConflict,
)
from agent_harness.storage import SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunRepository, RunTraceRepositoryConflict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loser_trace", "expected_exception"),
    [
        ("Trace-Queued-Race", None),
        ("Trace-Queued-Other", RunTraceIdempotencyConflict),
    ],
)
async def test_queued_idempotency_conflict_window_reconciles_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loser_trace: str,
    expected_exception: type[Exception] | None,
) -> None:
    """竞争窗口回读首次 queued run，并区分同 trace replay 与异 trace 冲突。"""

    dsn = sqlite_dsn(tmp_path / "queued-idempotency-race.db")
    run_migrations(dsn)
    loser_storage = SQLAlchemyStorage.from_dsn(dsn)
    winner_storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    identity = IdentityContext.local_default(session_id="queued-race-session")
    event_bus = persisted_event_bus(loser_storage, sink)
    loser = RunOrchestrator(storage=loser_storage, event_bus=event_bus, queue=queue)
    winner = RunOrchestrator(storage=winner_storage, event_bus=event_bus, queue=queue)

    async with loser_storage.uow() as uow:
        tenant = await uow.tenants.ensure(identity.tenant_id)
        await uow.sessions.ensure(
            SessionCreate(
                session_id=identity.session_id,
                tenant_id=tenant.id,
                user_id=identity.user_id,
                agent_id="fake-agent",
            )
        )
        await uow.commit()

    original_create_queued = RunRepository.create_queued
    winner_results: list[Any] = []
    injected = False

    async def inject_winner(
        repository: RunRepository,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """在 loser 建队前注入 winner，复现 repository 冲突后的回读窗口。"""

        nonlocal injected
        if not injected:
            injected = True
            winner_results.append(
                await winner.submit_run(
                    agent_id="fake-agent",
                    input={"source": "winner"},
                    idempotency_key="queued-race-key",
                    identity=identity,
                    request_id="request-winner",
                    trace_id="Trace-Queued-Race",
                )
            )
            raise RunTraceRepositoryConflict("trace.conflict")
        return await original_create_queued(repository, *args, **kwargs)

    monkeypatch.setattr(RunRepository, "create_queued", inject_winner)
    try:
        if expected_exception is None:
            replay = await loser.submit_run(
                agent_id="fake-agent",
                input={"source": "loser"},
                idempotency_key="queued-race-key",
                identity=identity,
                request_id="request-loser",
                trace_id=loser_trace,
            )
        else:
            with pytest.raises(expected_exception):
                await loser.submit_run(
                    agent_id="fake-agent",
                    input={"source": "loser"},
                    idempotency_key="queued-race-key",
                    identity=identity,
                    request_id="request-loser",
                    trace_id=loser_trace,
                )
            replay = winner_results[0]
        async with loser_storage.uow() as uow:
            runs = await uow.runs.list_for_tenant(identity.tenant_id)
    finally:
        await loser_storage.dispose()
        await winner_storage.dispose()

    assert len(winner_results) == 1
    assert replay.run_id == winner_results[0].run_id
    assert [run.id for run in runs] == [replay.run_id]
    assert queue.message_count == 1
    events = await sink.read(run_id=replay.run_id)
    assert len(events) == 1
    assert events[0].event_type == CanonicalEventType.RUN_QUEUED
    assert events[0].request_id == "request-winner"


@pytest.mark.asyncio
async def test_queued_trace_conflict_without_idempotent_winner_stays_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repository 报全局 trace 冲突且回读不到首次 run 时不得伪造 replay。"""

    dsn = sqlite_dsn(tmp_path / "queued-unowned-conflict.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    queue = InMemoryRunQueue()
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    identity = IdentityContext.local_default(session_id="queued-unowned-session")
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        queue=queue,
    )

    async def reject_trace(
        _repository: RunRepository,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        """持续模拟无归属 trace 冲突，确保 orchestrator 不会伪造重放结果。"""

        raise RunTraceRepositoryConflict("trace.conflict")

    monkeypatch.setattr(RunRepository, "create_queued", reject_trace)
    try:
        with pytest.raises(RunTraceConflict):
            await orchestrator.submit_run(
                agent_id="fake-agent",
                input={},
                idempotency_key="missing-winner-key",
                identity=identity,
                trace_id="Trace-Unowned",
            )
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant(identity.tenant_id)
    finally:
        await storage.dispose()

    assert runs == []
    assert queue.message_count == 0


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="Queued trace race contract requires an isolated PostgreSQL database.",
)
@pytest.mark.asyncio
async def test_postgresql_queued_same_key_same_trace_conflict_window_replays_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 PostgreSQL 独立 engine 也必须把 trace claim 窗口收敛到首次 run。"""

    async with postgres_database("agent_harness_queued_trace") as (dsn, _engine):
        await asyncio.to_thread(run_migrations, dsn)
        loser_storage = SQLAlchemyStorage.from_dsn(dsn)
        winner_storage = SQLAlchemyStorage.from_dsn(dsn)
        queue = InMemoryRunQueue()
        sink = LocalJsonlEventSink(tmp_path / "postgres-events.jsonl")
        identity = IdentityContext.local_default(session_id="queued-postgresql-session")
        event_bus = persisted_event_bus(loser_storage, sink)
        loser = RunOrchestrator(storage=loser_storage, event_bus=event_bus, queue=queue)
        winner = RunOrchestrator(storage=winner_storage, event_bus=event_bus, queue=queue)

        async with loser_storage.uow() as uow:
            tenant = await uow.tenants.ensure(identity.tenant_id)
            await uow.sessions.ensure(
                SessionCreate(
                    session_id=identity.session_id,
                    tenant_id=tenant.id,
                    user_id=identity.user_id,
                    agent_id="fake-agent",
                )
            )
            await uow.commit()

        original_create_queued = RunRepository.create_queued
        winner_results: list[Any] = []
        injected = False

        async def inject_winner(
            repository: RunRepository,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """在真实 PG 双连接中注入首个提交者，验证相同 race 的可重放收口。"""

            nonlocal injected
            if not injected:
                injected = True
                winner_results.append(
                    await winner.submit_run(
                        agent_id="fake-agent",
                        input={"source": "winner"},
                        idempotency_key="queued-postgresql-key",
                        identity=identity,
                        request_id="request-postgresql-winner",
                        trace_id="Trace-Queued-PostgreSQL",
                    )
                )
                raise RunTraceRepositoryConflict("trace.conflict")
            return await original_create_queued(repository, *args, **kwargs)

        monkeypatch.setattr(RunRepository, "create_queued", inject_winner)
        try:
            replay = await loser.submit_run(
                agent_id="fake-agent",
                input={"source": "loser"},
                idempotency_key="queued-postgresql-key",
                identity=identity,
                request_id="request-postgresql-loser",
                trace_id="Trace-Queued-PostgreSQL",
            )
            async with loser_storage.uow() as uow:
                runs = await uow.runs.list_for_tenant(identity.tenant_id)
        finally:
            await loser_storage.dispose()
            await winner_storage.dispose()

    assert len(winner_results) == 1
    assert replay.run_id == winner_results[0].run_id
    assert [run.id for run in runs] == [replay.run_id]
    assert queue.message_count == 1
    events = await sink.read(run_id=replay.run_id)
    assert len(events) == 1
    assert events[0].request_id == "request-postgresql-winner"
