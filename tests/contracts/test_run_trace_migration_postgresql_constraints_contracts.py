"""0013 canonical run trace 的 PostgreSQL 归属约束合同。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

from agent_harness.storage import run_migrations


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL trace migration contract runs when service smoke provides a DSN.",
)
@pytest.mark.asyncio
async def test_0013_postgresql_canonical_event_run_owner_rejects_direct_bypass() -> None:
    """fresh PostgreSQL schema 以联合外键锁定 event 的 run/tenant/trace 归属。"""

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    async def snapshot(connection: AsyncConnection) -> str:
        return str(
            (
                await connection.execute(
                    text(
                        "select coalesce(json_agg(row_to_json(event_row) order by id)::text, '[]') "
                        "from canonical_events event_row"
                    )
                )
            ).scalar_one()
        )

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_event_owner_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()

    dsn = test_url.render_as_string(hide_password=False)
    engine = create_async_engine(dsn)
    insert_event = text(
        "insert into canonical_events("
        "id, tenant_id, run_id, stream_id, event_type, seq, terminal, visibility, "
        "trace_id, record_scope, envelope_json) values ("
        ":event_id, :tenant_id, :run_id, :event_id, 'run.started', 1, false, "
        "'internal', :trace_id, :record_scope, '{}')"
    )
    try:
        await asyncio.to_thread(run_migrations, dsn)
        async with engine.connect() as connection:
            constraint_names = set(
                (
                    await connection.execute(
                        text(
                            "select conname from pg_constraint where conname in ("
                            "'uq_agent_runs_id_tenant_trace', "
                            "'fk_canonical_events_run_owner')"
                        )
                    )
                ).scalars()
            )
        assert constraint_names == {
            "uq_agent_runs_id_tenant_trace",
            "fk_canonical_events_run_owner",
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into tenants(id, display_name) values "
                    "('tenant-a', 'A'), ('tenant-b', 'B')"
                )
            )
            await connection.execute(
                text(
                    "insert into sessions(id, tenant_id, user_id, metadata_json) values "
                    "('session-a', 'tenant-a', 'user-a', '{}'), "
                    "('session-b', 'tenant-b', 'user-b', '{}')"
                )
            )
            for run_id, trace_id in (("root-a", "Trace-A"), ("root-b", "Trace-B")):
                await connection.execute(
                    text(
                        "insert into agent_runs("
                        "id, tenant_id, session_id, agent_id, status, trace_id, input_json, "
                        "execution_context_json) values (:run_id, 'tenant-a', 'session-a', "
                        "'agent-a', 'created', :trace_id, '{}', :execution_context)"
                    ).bindparams(sa.bindparam("execution_context", type_=sa.JSON())),
                    {
                        "run_id": run_id,
                        "trace_id": trace_id,
                        "execution_context": {"trace_id": trace_id},
                    },
                )
                await connection.execute(
                    text(
                        "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) "
                        "values (:trace_id, 'tenant-a', :run_id)"
                    ),
                    {"trace_id": trace_id, "run_id": run_id},
                )
            await connection.execute(
                insert_event,
                {
                    "event_id": "event-valid-run",
                    "tenant_id": "tenant-a",
                    "run_id": "root-a",
                    "trace_id": "Trace-A",
                    "record_scope": "run",
                },
            )
            await connection.execute(
                insert_event,
                {
                    "event_id": "event-valid-non-run",
                    "tenant_id": "tenant-b",
                    "run_id": None,
                    "trace_id": None,
                    "record_scope": "non_run",
                },
            )

        for event_id, tenant_id, run_id, trace_id in (
            ("event-cross-tenant", "tenant-b", "root-a", "Trace-A"),
            ("event-wrong-trace", "tenant-a", "root-a", "Trace-B"),
            ("event-orphan-run", "tenant-a", "missing-run", "Trace-A"),
        ):
            async with engine.connect() as connection:
                before = await snapshot(connection)
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        insert_event,
                        {
                            "event_id": event_id,
                            "tenant_id": tenant_id,
                            "run_id": run_id,
                            "trace_id": trace_id,
                            "record_scope": "run",
                        },
                    )
            async with engine.connect() as connection:
                assert await snapshot(connection) == before
                assert (
                    await connection.execute(
                        text("select count(*) from canonical_events where id=:event_id"),
                        {"event_id": event_id},
                    )
                ).scalar_one() == 0

        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "select id, run_id, trace_id, record_scope "
                        "from canonical_events order by id"
                    )
                )
            ).all() == [
                ("event-valid-non-run", None, None, "non_run"),
                ("event-valid-run", "root-a", "Trace-A", "run"),
            ]
    finally:
        await engine.dispose()
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()
