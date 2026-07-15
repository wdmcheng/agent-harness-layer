"""0013a 已发布 shape 的 PostgreSQL 前滚与运行前门禁合同。"""

from __future__ import annotations

import asyncio
import os

import pytest
import sqlalchemy as sa
from alembic import command
from tests.contracts.run_trace_revision_hardening_helpers import (
    CHECK_TARGETS,
    REVISION_0013,
    REVISION_0013A,
    migration_config,
)
from tests.contracts.run_trace_revision_hardening_postgresql_helpers import (
    postgres_database,
    postgres_full_snapshot,
    postgres_side_effect_snapshot,
    replace_postgresql_check,
    replace_postgresql_scope_check_with_bpchar,
    seed_legacy_postgresql_rows,
    simulate_legacy_postgresql_0013,
)

from agent_harness.storage import SQLAlchemyStorage, get_head_revision, run_migrations
from agent_harness.storage.migrations.runner import (
    SchemaMigrationRequiredError,
    require_migration_head,
)


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL revision hardening contract requires a service test DSN.",
)
@pytest.mark.asyncio
async def test_fresh_postgresql_0012a_reaches_hardened_head() -> None:
    """fresh PostgreSQL 必须沿 0012a -> 0013 -> 0013a -> 当前 head 得到最终 shape。"""

    async with postgres_database("agent_harness_trace_fresh") as (dsn, engine):
        expected_head = get_head_revision()
        await asyncio.to_thread(run_migrations, dsn, "0012a_embedding_cache_tenant_scope")
        await asyncio.to_thread(run_migrations, dsn)
        assert await asyncio.to_thread(require_migration_head, dsn) == expected_head
        async with engine.connect() as connection:
            revision = (
                await connection.execute(sa.text("select version_num from alembic_version"))
            ).scalar_one()
            constraints = set(
                (
                    await connection.execute(
                        sa.text(
                            "select conname from pg_constraint where conname in ("
                            "'uq_agent_runs_id_tenant_trace', "
                            "'fk_canonical_events_run_owner', "
                            "'ck_canonical_events_record_scope', "
                            "'ck_canonical_events_run_ownership', "
                            "'ck_canonical_events_non_run_ownership', "
                            "'ck_audit_logs_record_scope', "
                            "'uq_canonical_events_tenant_stream_seq')"
                        )
                    )
                ).scalars()
            )
        assert revision == expected_head
        assert constraints == {
            "uq_agent_runs_id_tenant_trace",
            "fk_canonical_events_run_owner",
            "ck_canonical_events_record_scope",
            "ck_canonical_events_run_ownership",
            "ck_canonical_events_non_run_ownership",
            "ck_audit_logs_record_scope",
            "uq_canonical_events_tenant_stream_seq",
        }


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL revision hardening contract requires a service test DSN.",
)
@pytest.mark.parametrize(("table", "name"), CHECK_TARGETS)
@pytest.mark.asyncio
async def test_same_named_weakened_postgresql_check_fails_without_side_effects(
    table: str,
    name: str,
) -> None:
    """真实 PostgreSQL 同名弱 CHECK 不能通过反射分类或写入 head stamp。"""

    async with postgres_database("agent_harness_weak_check") as (
        dsn,
        engine,
    ):
        await asyncio.to_thread(run_migrations, dsn, REVISION_0013)
        await replace_postgresql_check(engine, table=table, name=name)
        before = await postgres_full_snapshot(engine)

        with pytest.raises(RuntimeError, match="incompatible or partial 0013 schema shape"):
            await asyncio.to_thread(run_migrations, dsn)

        assert await postgres_full_snapshot(engine) == before


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL revision hardening contract requires a service test DSN.",
)
@pytest.mark.parametrize(
    ("table", "name"),
    (
        ("canonical_events", "ck_canonical_events_record_scope"),
        ("audit_logs", "ck_audit_logs_record_scope"),
    ),
)
@pytest.mark.asyncio
async def test_bpchar_scope_semantic_change_is_rejected_before_postgresql_side_effects(
    table: str,
    name: str,
) -> None:
    """bpchar 会放行 `run `；0013a 必须按定义拒绝并保留现场完整证据。"""

    async with postgres_database("agent_harness_bpchar_check") as (dsn, engine):
        await asyncio.to_thread(run_migrations, dsn, REVISION_0013)
        await replace_postgresql_scope_check_with_bpchar(engine, table=table, name=name)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
            )
            if table == "canonical_events":
                await connection.execute(
                    sa.text(
                        "insert into canonical_events(id, tenant_id, run_id, stream_id, "
                        "event_type, seq, terminal, visibility, trace_id, record_scope) values "
                        "('bpchar-event', 'tenant-a', null, 'bpchar-stream', 'run.started', "
                        "1, false, 'internal', null, 'run ')"
                    )
                )
            else:
                await connection.execute(
                    sa.text(
                        "insert into audit_logs(id, tenant_id, action, payload_json, "
                        "record_scope) values "
                        "('bpchar-audit', 'tenant-a', 'boundary-check', '{}', 'run ')"
                    )
                )
        async with engine.connect() as connection:
            invalid = (
                await connection.execute(
                    sa.text(
                        f'select count(*) from "{table}" '
                        "where record_scope not in ('run', 'non_run')"
                    )
                )
            ).scalar_one()
        assert invalid == 1
        before = await postgres_full_snapshot(engine)

        with pytest.raises(RuntimeError, match="incompatible or partial 0013 schema shape"):
            await asyncio.to_thread(run_migrations, dsn)

        assert await postgres_full_snapshot(engine) == before


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL revision hardening contract requires a service test DSN.",
)
@pytest.mark.asyncio
async def test_old_postgresql_0013_is_hardened_before_event_writes() -> None:
    """真实旧 0013 先被 head gate 拒绝，前滚后 event 写入与硬约束全部成立。"""

    from sqlalchemy.exc import IntegrityError

    from agent_harness.events import CanonicalEvent, CanonicalEventType, PostgreSQLEventSink

    async with postgres_database("agent_harness_trace_legacy") as (dsn, engine):
        expected_head = get_head_revision()
        await asyncio.to_thread(run_migrations, dsn, REVISION_0013)
        await simulate_legacy_postgresql_0013(engine)
        await seed_legacy_postgresql_rows(engine)
        before = await postgres_side_effect_snapshot(engine)

        with pytest.raises(SchemaMigrationRequiredError):
            await asyncio.to_thread(require_migration_head, dsn)
        assert await postgres_side_effect_snapshot(engine) == before

        await asyncio.to_thread(run_migrations, dsn, REVISION_0013A)
        with pytest.raises(SchemaMigrationRequiredError):
            await asyncio.to_thread(require_migration_head, dsn)
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "select id, run_id, stream_id, trace_id, record_scope "
                        "from canonical_events order by id"
                    )
                )
            ).all()
            constraint_names = set(
                (
                    await connection.execute(
                        sa.text(
                            "select conname from pg_constraint where conname in ("
                            "'uq_agent_runs_id_tenant_trace', "
                            "'fk_canonical_events_run_owner', "
                            "'ck_canonical_events_record_scope', "
                            "'ck_canonical_events_run_ownership', "
                            "'ck_canonical_events_non_run_ownership', "
                            "'ck_audit_logs_record_scope', "
                            "'uq_canonical_events_tenant_stream_seq')"
                        )
                    )
                ).scalars()
            )
        assert rows == [
            ("legacy-non-run", None, "root-a", "Trace-A", "non_run"),
            ("legacy-run", "root-a", "root-a", "Trace-A", "run"),
        ]
        assert len(constraint_names) == 7

        before_0013a_downgrade = await postgres_full_snapshot(engine)
        await asyncio.to_thread(command.downgrade, migration_config(dsn), REVISION_0013)
        after_stamp_downgrade = await postgres_full_snapshot(engine)
        assert after_stamp_downgrade[:4] == before_0013a_downgrade[:4]
        assert after_stamp_downgrade[4] == REVISION_0013

        with pytest.raises(RuntimeError, match="0013 downgrade refused: explicit opt-in"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn),
                "0012a_embedding_cache_tenant_scope",
            )
        with pytest.raises(RuntimeError, match="0013 downgrade refused: canonical trace evidence"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
                "0012a_embedding_cache_tenant_scope",
            )
        assert await postgres_full_snapshot(engine) == after_stamp_downgrade

        await asyncio.to_thread(run_migrations, dsn)
        assert await asyncio.to_thread(require_migration_head, dsn) == expected_head

        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            sink = PostgreSQLEventSink(storage)
            persisted = await sink.write(
                CanonicalEvent(
                    event_id="post-hardening-event",
                    tenant_id="tenant-a",
                    run_id="root-a",
                    event_type=CanonicalEventType.RUN_STARTED,
                    seq=0,
                    trace_id="Trace-A",
                )
            )
            assert persisted.seq == 3
        finally:
            await storage.dispose()

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "insert into canonical_events(id, tenant_id, run_id, stream_id, "
                        "event_type, seq, terminal, visibility, trace_id, record_scope) values "
                        "('bad-owner', 'tenant-a', 'root-a', 'bad-owner', 'run.started', "
                        "1, false, 'internal', 'Trace-B', 'run')"
                    )
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "insert into audit_logs(id, tenant_id, action, payload_json, record_scope) "
                        "values ('bad-audit', 'tenant-a', 'bad', '{}', 'other')"
                    )
                )

        before_0014_downgrade = await postgres_full_snapshot(engine)
        with pytest.raises(RuntimeError, match="0014 downgrade refused: explicit opt-in"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn),
                REVISION_0013A,
            )
        with pytest.raises(RuntimeError, match="0014 downgrade refused: evidence exists"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
                REVISION_0013A,
            )
        assert await postgres_full_snapshot(engine) == before_0014_downgrade
