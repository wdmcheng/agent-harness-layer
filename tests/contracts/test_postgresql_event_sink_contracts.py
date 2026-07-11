"""PostgreSQL CanonicalEvent sink 的 envelope、序列与唯一终态合同。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    PostgreSQLEventSink,
    TerminalEventError,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_postgresql_event_sink_contract_round_trips_full_envelope(tmp_path: Path) -> None:
    """SQLite跑同一 adapter seam；真实 PostgreSQL并发证据由条件测试补齐。"""

    dsn = _dsn(tmp_path / "events.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-event")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id="tenant-event", user_id="user-event", agent_id="agent-event"
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-event",
                    session_id=session.id,
                    agent_id="agent-event",
                )
            )
            await uow.commit()
        sink = PostgreSQLEventSink(storage)
        bus = EventBus(sink=sink)
        queued = await bus.publish(
            tenant_id="tenant-event",
            user_id="user-event",
            agent_id="agent-event",
            run_id=run.id,
            event_type=CanonicalEventType.RUN_QUEUED,
            payload={"source_ref": "source://event", "trust_level": "trusted"},
            request_id="req-event",
            trace_id="trace-event",
            event_id=f"run-queued:{run.id}",
        )
        retry = await bus.publish(
            tenant_id="tenant-event",
            user_id="user-event",
            agent_id="agent-event",
            run_id=run.id,
            event_type=CanonicalEventType.RUN_QUEUED,
            event_id=f"run-queued:{run.id}",
        )
        terminal = await bus.publish(
            tenant_id="tenant-event",
            run_id=run.id,
            event_type=CanonicalEventType.RUN_COMPLETED,
            terminal=True,
            event_id=f"run-completed:{run.id}",
        )
        events = await sink.read(run_id=run.id)

        assert retry == queued
        assert terminal.seq == 2
        assert [event.seq for event in events] == [1, 2]
        assert events[0].to_payload() == queued.to_payload()
        with pytest.raises(TerminalEventError):
            await bus.publish(
                tenant_id="tenant-event",
                run_id=run.id,
                event_type=CanonicalEventType.RUN_FAILED,
                terminal=True,
            )
    finally:
        await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL并发 event sink 合同由 service环境注入DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_event_sink_serializes_cross_instance_sequences() -> None:
    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    tenant_id = f"event-{uuid4()}"
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_id)
            session = await uow.sessions.create(
                SessionCreate(tenant_id=tenant_id, user_id="user", agent_id="agent")
            )
            run = await uow.runs.create(
                RunCreate(tenant_id=tenant_id, session_id=session.id, agent_id="agent")
            )
            await uow.commit()
        buses = [EventBus(sink=PostgreSQLEventSink(storage)) for _ in range(6)]
        events = await asyncio.gather(
            *(
                bus.publish(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    event_type=CanonicalEventType.RUN_STARTED,
                    event_id=f"event-{run.id}-{index}",
                )
                for index, bus in enumerate(buses)
            )
        )
        assert sorted(event.seq for event in events) == [1, 2, 3, 4, 5, 6]

        terminals = await asyncio.gather(
            *(
                bus.publish(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    event_type=CanonicalEventType.RUN_COMPLETED,
                    terminal=True,
                    event_id=f"terminal-{run.id}-{index}",
                )
                for index, bus in enumerate(buses[:2])
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, TerminalEventError) for item in terminals) == 1
    finally:
        await storage.dispose()
