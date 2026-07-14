"""PostgreSQL CanonicalEvent sink 的 envelope、序列与 non-run 合同。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    PostgreSQLEventSink,
    TerminalEventError,
)
from agent_harness.observability import TelemetryContext, TelemetryFacade, TelemetryRecord
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


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
                    trace_id="trace-event",
                )
            )
            await uow.commit()
        sink = PostgreSQLEventSink(storage)
        bus = EventBus(
            sink=sink,
            run_trace_resolver=StorageRunTraceResolver(storage),
        )
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
            payload={"source_ref": "source://event", "trust_level": "trusted"},
            request_id="req-event",
            event_id=f"run-queued:{run.id}",
            trace_id="trace-event",
        )
        terminal = await bus.publish(
            tenant_id="tenant-event",
            run_id=run.id,
            event_type=CanonicalEventType.RUN_COMPLETED,
            terminal=True,
            visibility="public",
            trace_id="trace-event",
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
                visibility="public",
                trace_id="trace-event",
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
                RunCreate(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    agent_id="agent",
                    trace_id=f"trace-{tenant_id}",
                )
            )
            await uow.commit()
        buses = [
            EventBus(
                sink=PostgreSQLEventSink(storage),
                run_trace_resolver=StorageRunTraceResolver(storage),
            )
            for _ in range(6)
        ]
        events = await asyncio.gather(
            *(
                bus.publish(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    event_type=CanonicalEventType.RUN_STARTED,
                    event_id=f"event-{run.id}-{index}",
                    trace_id=f"trace-{tenant_id}",
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
                    visibility="public",
                    event_id=f"terminal-{run.id}-{index}",
                    trace_id=f"trace-{tenant_id}",
                )
                for index, bus in enumerate(buses[:2])
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, TerminalEventError) for item in terminals) == 1
    finally:
        await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL service telemetry non-run 合同由 service 环境注入 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_service_telemetry_persists_non_run_without_fake_lineage() -> None:
    """service composition 的 PG sink 接受无 run/trace telemetry，且不伪造 AgentRun。"""

    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_non_run_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()
    dsn = test_url.render_as_string(hide_password=False)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    tenant_a = f"telemetry-a-{uuid4()}"
    tenant_b = f"telemetry-b-{uuid4()}"
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_a)
            await uow.tenants.ensure(tenant_b)
            await uow.commit()

        async def publish(tenant_id: str, ordinal: int) -> None:
            facade = TelemetryFacade(local_sink=PostgreSQLEventSink(storage))
            result = await facade.publish_record(
                TelemetryRecord(
                    name=f"service.health.{ordinal}",
                    context=TelemetryContext(tenant_id=tenant_id),
                    payload={"ordinal": ordinal},
                )
            )
            assert result.local_status.status == "written"

        await asyncio.gather(
            publish(tenant_a, 1),
            publish(tenant_a, 2),
            publish(tenant_b, 1),
        )

        async with storage.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "select tenant_id, run_id, stream_id, trace_id, record_scope, "
                            "seq, envelope_json from canonical_events "
                            "where tenant_id in (:tenant_a, :tenant_b) "
                            "order by tenant_id, seq"
                        ),
                        {"tenant_a": tenant_a, "tenant_b": tenant_b},
                    )
                )
                .mappings()
                .all()
            )
            fake_runs = int(
                (
                    await connection.execute(
                        text(
                            "select count(*) from agent_runs "
                            "where tenant_id in (:tenant_a, :tenant_b)"
                        ),
                        {"tenant_a": tenant_a, "tenant_b": tenant_b},
                    )
                ).scalar_one()
            )
        assert [(row["tenant_id"], row["seq"]) for row in rows] == [
            (tenant_a, 1),
            (tenant_a, 2),
            (tenant_b, 1),
        ]
        assert all(row["run_id"] is None for row in rows)
        assert all(row["stream_id"] == "telemetry" for row in rows)
        assert all(row["trace_id"] is None for row in rows)
        assert all(row["record_scope"] == "non_run" for row in rows)
        assert all(row["envelope_json"]["run_id"] == "telemetry" for row in rows)
        assert fake_runs == 0
    finally:
        await storage.dispose()
        cleanup_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with cleanup_engine.connect() as connection:
            await connection.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname=:database_name and pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        await cleanup_engine.dispose()
