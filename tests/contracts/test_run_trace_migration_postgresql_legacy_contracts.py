"""0013 canonical run trace 的 PostgreSQL backfill 与读取合同。"""

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
async def test_0013_postgresql_rebuilds_and_replays_0011_canonical_event() -> None:
    """真实 PostgreSQL 从 0011 升级后仍可读取并幂等重放原 event-id。"""

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from agent_harness.events import CanonicalEvent, PostgreSQLEventSink
    from agent_harness.storage import SQLAlchemyStorage

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_trace_legacy_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()

    dsn = test_url.render_as_string(hide_password=False)
    engine = create_async_engine(dsn)
    try:
        await asyncio.to_thread(
            run_migrations,
            dsn,
            "0011_eval_experiment_legacy_created_review",
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
            )
            await connection.execute(
                text(
                    "insert into sessions(id, tenant_id, user_id, metadata_json) "
                    "values ('session-a', 'tenant-a', 'user-a', '{}')"
                )
            )
            await connection.execute(
                text(
                    "insert into agent_runs("
                    "id, tenant_id, session_id, agent_id, status, input_json"
                    ") values ('legacy-run', 'tenant-a', 'session-a', 'agent-a', "
                    "'created', '{}')"
                )
            )
            await connection.execute(
                text(
                    "insert into canonical_events("
                    "id, tenant_id, run_id, agent_id, event_type, seq, terminal, visibility, "
                    "payload_json, payload_ref, request_id, trace_id, created_at"
                    ") values ('legacy-event', 'tenant-a', 'legacy-run', 'agent-a', "
                    "'run.started', 1, false, 'public', '{\"input\":\"preserved\"}', "
                    "'artifact://legacy-payload', 'request-legacy', null, "
                    "'2026-01-01T00:00:00Z')"
                )
            )

        await engine.dispose()
        await asyncio.to_thread(run_migrations, dsn)
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            envelope = (
                await connection.execute(
                    text("select envelope_json from canonical_events where id='legacy-event'")
                )
            ).scalar_one()
        migrated = CanonicalEvent.model_validate(envelope)
        assert migrated.event_id == "legacy-event"
        assert migrated.tenant_id == "tenant-a"
        assert migrated.run_id == "legacy-run"
        assert migrated.agent_id == "agent-a"
        assert migrated.event_type.value == "run.started"
        assert migrated.seq == 1
        assert migrated.timestamp.isoformat() == "2026-01-01T00:00:00+00:00"
        assert migrated.payload == {"input": "preserved"}
        assert migrated.payload_ref == "artifact://legacy-payload"
        assert migrated.request_id == "request-legacy"
        assert migrated.trace_id is not None

        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            sink = PostgreSQLEventSink(storage)
            events = await sink.read(run_id="legacy-run")
            assert events == [migrated]
            assert await sink.write(events[0]) == migrated
        finally:
            await storage.dispose()
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("select count(*) from canonical_events where id='legacy-event'")
                )
            ).scalar_one() == 1
    finally:
        await engine.dispose()
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL trace migration contract runs when service smoke provides a DSN.",
)
@pytest.mark.asyncio
async def test_0013_postgresql_backfill_and_direct_constraint_bypass() -> None:
    """隔离 PostgreSQL database 验证真实 DDL/backfill 与复合 FK 硬门禁。"""

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_trace_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()

    dsn = test_url.render_as_string(hide_password=False)
    engine = create_async_engine(dsn)
    try:
        await asyncio.to_thread(
            run_migrations,
            dsn,
            "0012a_embedding_cache_tenant_scope",
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
            )
            await connection.execute(
                text(
                    "insert into sessions(id, tenant_id, user_id, metadata_json) "
                    "values ('session-a', 'tenant-a', 'user-a', '{}')"
                )
            )
            non_run_payload = {
                "telemetry": {
                    "name": "legacy.non-run",
                    "record_type": "event",
                    "context": {"tenant_id": "tenant-a", "run_id": None, "trace_id": None},
                    "payload": {},
                    "payload_ref": None,
                }
            }
            run_payload = {
                "telemetry": {
                    "name": "legacy.run",
                    "record_type": "event",
                    "context": {
                        "tenant_id": "tenant-a",
                        "run_id": "root-a",
                        "trace_id": None,
                    },
                    "payload": {},
                    "payload_ref": None,
                }
            }
            await connection.exec_driver_sql("alter table canonical_events disable trigger all")
            try:
                await connection.execute(
                    text(
                        "insert into canonical_events("
                        "id, tenant_id, run_id, event_type, seq, terminal, visibility, "
                        "payload_json, trace_id, envelope_json) values "
                        "('legacy-pg-non-run', 'tenant-a', 'telemetry', 'artifact.created', "
                        "1, false, 'internal', :non_run_payload, 'legacy-trace', "
                        ":non_run_envelope), "
                        "('legacy-pg-run', 'tenant-a', 'telemetry', 'artifact.created', "
                        "2, false, 'internal', :run_payload, null, :run_envelope)"
                    ).bindparams(
                        sa.bindparam("non_run_payload", type_=sa.JSON()),
                        sa.bindparam("non_run_envelope", type_=sa.JSON()),
                        sa.bindparam("run_payload", type_=sa.JSON()),
                        sa.bindparam("run_envelope", type_=sa.JSON()),
                    ),
                    {
                        "non_run_payload": non_run_payload,
                        "non_run_envelope": {
                            "event_id": "legacy-pg-non-run",
                            "tenant_id": "tenant-a",
                            "run_id": "telemetry",
                            "event_type": "artifact.created",
                            "seq": 1,
                            "timestamp": "2026-01-01T00:00:00Z",
                            "terminal": False,
                            "visibility": "internal",
                            "trace_id": "legacy-trace",
                            "payload": non_run_payload,
                        },
                        "run_payload": run_payload,
                        "run_envelope": {
                            "event_id": "legacy-pg-run",
                            "tenant_id": "tenant-a",
                            "run_id": "telemetry",
                            "event_type": "artifact.created",
                            "seq": 2,
                            "timestamp": "2026-01-01T00:00:00Z",
                            "terminal": False,
                            "visibility": "internal",
                            "trace_id": None,
                            "payload": run_payload,
                        },
                    },
                )
            finally:
                await connection.exec_driver_sql("alter table canonical_events enable trigger all")
            await connection.execute(
                text(
                    "insert into agent_runs(id, tenant_id, session_id, agent_id, status, "
                    "input_json, execution_context_json) values "
                    "('root-a', 'tenant-a', 'session-a', 'agent-a', 'created', '{}', "
                    '\'{"trace_id":"Trace-PG"}\')'
                )
            )
        await engine.dispose()
        await asyncio.to_thread(run_migrations, dsn)
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            projection = (
                await connection.execute(text("select trace_id from agent_runs where id='root-a'"))
            ).scalar_one()
            binding = (
                await connection.execute(
                    text(
                        "select tenant_id, root_run_id from run_trace_bindings "
                        "where trace_id='Trace-PG'"
                    )
                )
            ).one()
            canonical_trace_nullable = (
                await connection.execute(
                    text(
                        "select is_nullable from information_schema.columns "
                        "where table_schema='public' and table_name='canonical_events' "
                        "and column_name='trace_id'"
                    )
                )
            ).scalar_one()
            migrated_telemetry = (
                (
                    await connection.execute(
                        text(
                            "select id, run_id, stream_id, trace_id, record_scope, envelope_json "
                            "from canonical_events where id like 'legacy-pg-%' order by id"
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert projection == "Trace-PG"
        assert tuple(binding) == ("tenant-a", "root-a")
        assert canonical_trace_nullable == "YES"
        assert [
            (
                row["id"],
                row["run_id"],
                row["stream_id"],
                row["trace_id"],
                row["record_scope"],
            )
            for row in migrated_telemetry
        ] == [
            ("legacy-pg-non-run", None, "telemetry", "legacy-trace", "non_run"),
            ("legacy-pg-run", "root-a", "telemetry", "Trace-PG", "run"),
        ]
        assert migrated_telemetry[0]["envelope_json"]["trace_id"] == "legacy-trace"
        assert migrated_telemetry[1]["envelope_json"]["trace_id"] == "Trace-PG"

        from agent_harness.events import CanonicalEvent, CanonicalEventType, PostgreSQLEventSink
        from agent_harness.storage import SQLAlchemyStorage

        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            sink = PostgreSQLEventSink(storage)
            non_run = CanonicalEvent(
                event_id="pg-non-run-null-trace",
                tenant_id="tenant-a",
                run_id="root-a",
                event_type=CanonicalEventType.ARTIFACT_CREATED,
                seq=0,
                trace_id=None,
                record_scope="non_run",
            )
            persisted = await sink.write(non_run)
            assert persisted.trace_id is None

            invalid = non_run.model_copy(update={"event_id": "pg-invalid-scope"})
            object.__setattr__(invalid, "record_scope", "other")
            with pytest.raises(ValueError, match="record_scope must be run or non_run"):
                await sink.write(invalid)
            assert [event.event_id for event in await sink.read(run_id="root-a")] == [
                "legacy-pg-run"
            ]
        finally:
            await storage.dispose()

        async with engine.connect() as connection:
            persisted_non_run = (
                await connection.execute(
                    text(
                        "select run_id, stream_id, record_scope from canonical_events "
                        "where id='pg-non-run-null-trace'"
                    )
                )
            ).one()
        assert tuple(persisted_non_run) == (None, "root-a", "non_run")

        for event_id, trace_id, record_scope in (
            ("pg-invalid-scope-row", None, "other"),
            ("pg-run-null-trace-row", None, "run"),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "insert into canonical_events("
                            "id, tenant_id, run_id, stream_id, event_type, seq, terminal, "
                            "visibility, trace_id, record_scope, envelope_json) values ("
                            ":event_id, 'tenant-a', 'root-a', 'root-a', 'artifact.created', "
                            "99, false, "
                            "'internal', :trace_id, :record_scope, '{}')"
                        ),
                        {
                            "event_id": event_id,
                            "trace_id": trace_id,
                            "record_scope": record_scope,
                        },
                    )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "insert into audit_logs("
                        "id, tenant_id, action, payload_json, record_scope"
                        ") values ('pg-invalid-audit-scope', 'tenant-a', 'invalid', "
                        "'{}', 'other')"
                    )
                )
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("select count(*) from audit_logs where id='pg-invalid-audit-scope'")
                )
            ).scalar_one() == 0

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("insert into tenants(id, display_name) values ('tenant-b', 'B')")
                )
                await connection.execute(
                    text(
                        "insert into sessions(id, tenant_id, user_id, metadata_json) "
                        "values ('session-b', 'tenant-b', 'user-b', '{}')"
                    )
                )
                await connection.execute(
                    text(
                        "insert into agent_runs(id, tenant_id, session_id, agent_id, status, "
                        "parent_run_id, trace_id, input_json, execution_context_json) values "
                        "('child-b', 'tenant-b', 'session-b', 'agent-a', 'created', "
                        "'root-a', 'Trace-PG', '{}', '{\"trace_id\":\"Trace-PG\"}')"
                    )
                )
    finally:
        await engine.dispose()
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()
