"""0013a run trace 事件硬化合同共享的 PostgreSQL 迁移 fixture。"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.contracts.run_trace_revision_hardening_helpers import CHECK_TARGETS


@asynccontextmanager
async def postgres_database(prefix: str) -> AsyncGenerator[tuple[str, AsyncEngine]]:
    """创建并最终强制删除带随机后缀的 PostgreSQL 数据库，隔离 DDL 硬化实验及其连接。"""

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"{prefix}_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()

    dsn = test_url.render_as_string(hide_password=False)
    engine = create_async_engine(dsn)
    try:
        yield dsn, engine
    finally:
        await engine.dispose()
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()


async def simulate_legacy_postgresql_0013(engine: AsyncEngine) -> None:
    """将当前数据库改造为旧 revision 的约束和列形状，模拟生产升级前的真实兼容输入。"""

    async with engine.begin() as connection:
        for constraint in (
            "fk_canonical_events_run_owner",
            "ck_canonical_events_non_run_ownership",
            "ck_canonical_events_run_ownership",
            "ck_canonical_events_record_scope",
            "uq_canonical_events_tenant_stream_seq",
        ):
            await connection.exec_driver_sql(
                f'alter table canonical_events drop constraint "{constraint}"'
            )
        await connection.exec_driver_sql("drop index ix_canonical_events_stream_id")
        await connection.exec_driver_sql(
            "alter table canonical_events alter column trace_id set not null"
        )
        await connection.exec_driver_sql(
            "alter table canonical_events alter column run_id set not null"
        )
        await connection.exec_driver_sql("alter table canonical_events drop column stream_id")
        await connection.exec_driver_sql(
            "alter table canonical_events add constraint uq_canonical_events_run_seq "
            "unique (run_id, seq)"
        )
        await connection.exec_driver_sql(
            "alter table audit_logs drop constraint ck_audit_logs_record_scope"
        )
        await connection.exec_driver_sql(
            "alter table agent_runs drop constraint uq_agent_runs_id_tenant_trace"
        )


async def seed_legacy_postgresql_rows(engine: AsyncEngine) -> None:
    """向旧 PostgreSQL schema 写入 run/non-run 事件和审计行，验证硬化升级的保留语义。"""

    async with engine.begin() as connection:
        await connection.execute(
            sa.text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
        )
        await connection.execute(
            sa.text(
                "insert into sessions(id, tenant_id, user_id, metadata_json) "
                "values ('session-a', 'tenant-a', 'user-a', '{}')"
            )
        )
        await connection.execute(
            sa.text(
                "insert into agent_runs(id, tenant_id, session_id, agent_id, status, trace_id, "
                "input_json, execution_context_json) values ('root-a', 'tenant-a', "
                "'session-a', 'agent-a', 'created', 'Trace-A', '{}', :context)"
            ).bindparams(sa.bindparam("context", type_=sa.JSON())),
            {"context": {"trace_id": "Trace-A"}},
        )
        await connection.execute(
            sa.text(
                "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) "
                "values ('Trace-A', 'tenant-a', 'root-a')"
            )
        )
        statement = sa.text(
            "insert into canonical_events(id, tenant_id, run_id, event_type, seq, terminal, "
            "visibility, trace_id, envelope_json, record_scope) values "
            "(:event_id, 'tenant-a', 'root-a', 'run.started', :seq, false, 'internal', "
            "'Trace-A', :envelope, :record_scope)"
        ).bindparams(sa.bindparam("envelope", type_=sa.JSON()))
        for event_id, seq, record_scope in (
            ("legacy-run", 1, "run"),
            ("legacy-non-run", 2, "non_run"),
        ):
            await connection.execute(
                statement,
                {
                    "event_id": event_id,
                    "seq": seq,
                    "record_scope": record_scope,
                    "envelope": {
                        "event_id": event_id,
                        "tenant_id": "tenant-a",
                        "run_id": "root-a",
                        "event_type": "run.started",
                        "seq": seq,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "terminal": False,
                        "visibility": "internal",
                        "trace_id": "Trace-A",
                        "record_scope": record_scope,
                        "payload": {},
                    },
                },
            )
        await connection.execute(
            sa.text(
                "insert into audit_logs(id, tenant_id, action, payload_json, record_scope) "
                "values ('audit-a', 'tenant-a', 'run.started', '{}', 'run')"
            )
        )


async def postgres_side_effect_snapshot(engine: AsyncEngine) -> tuple[str, int, int]:
    """读取 revision、run 与事件计数的最小副作用快照，用于断言预检失败没有改动数据。"""

    async with engine.connect() as connection:
        revision = (
            await connection.execute(sa.text("select version_num from alembic_version"))
        ).scalar_one()
        run_count = (
            await connection.execute(sa.text("select count(*) from agent_runs"))
        ).scalar_one()
        event_count = (
            await connection.execute(sa.text("select count(*) from canonical_events"))
        ).scalar_one()
    return str(revision), int(run_count), int(event_count)


async def postgres_full_snapshot(engine: AsyncEngine) -> tuple[object, ...]:
    """捕获相关表完整 column/constraint/index catalog、全行数据与 revision。"""

    tables = ("agent_runs", "audit_logs", "canonical_events", "run_trace_bindings")
    async with engine.connect() as connection:
        columns = tuple(
            (
                await connection.execute(
                    sa.text(
                        "select relation.relname, attribute.attnum, attribute.attname, "
                        "pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
                        "attribute.attnotnull, "
                        "pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid) "
                        "from pg_catalog.pg_class relation "
                        "join pg_catalog.pg_namespace namespace "
                        "on namespace.oid=relation.relnamespace "
                        "join pg_catalog.pg_attribute attribute "
                        "on attribute.attrelid=relation.oid "
                        "left join pg_catalog.pg_attrdef default_value "
                        "on default_value.adrelid=relation.oid "
                        "and default_value.adnum=attribute.attnum "
                        "where namespace.nspname=current_schema() "
                        "and relation.relname in "
                        "('agent_runs','audit_logs','canonical_events','run_trace_bindings') "
                        "and attribute.attnum > 0 and not attribute.attisdropped "
                        "order by relation.relname, attribute.attnum"
                    )
                )
            ).tuples()
        )
        constraints = tuple(
            (
                await connection.execute(
                    sa.text(
                        "select relation.relname, constraint_row.conname, "
                        "constraint_row.contype, "
                        "pg_catalog.pg_get_constraintdef(constraint_row.oid, true) "
                        "from pg_catalog.pg_constraint constraint_row "
                        "join pg_catalog.pg_class relation "
                        "on relation.oid=constraint_row.conrelid "
                        "join pg_catalog.pg_namespace namespace "
                        "on namespace.oid=relation.relnamespace "
                        "where namespace.nspname=current_schema() "
                        "and relation.relname in "
                        "('agent_runs','audit_logs','canonical_events','run_trace_bindings') "
                        "order by relation.relname, constraint_row.conname"
                    )
                )
            ).tuples()
        )
        indexes = tuple(
            (
                await connection.execute(
                    sa.text(
                        "select tablename, indexname, indexdef from pg_catalog.pg_indexes "
                        "where schemaname=current_schema() and tablename in "
                        "('agent_runs','audit_logs','canonical_events','run_trace_bindings') "
                        "order by tablename, indexname"
                    )
                )
            ).tuples()
        )
        data: list[tuple[str, tuple[str, ...]]] = []
        for table in tables:
            rows = (
                await connection.execute(
                    sa.text(
                        f"select row_to_json(snapshot_row)::text from "
                        f'(select * from "{table}") snapshot_row '
                        "order by row_to_json(snapshot_row)::text"
                    )
                )
            ).scalars()
            data.append((table, tuple(str(row) for row in rows)))
        revision = (
            await connection.execute(sa.text("select version_num from alembic_version"))
        ).scalar_one()
    return columns, constraints, indexes, tuple(data), str(revision)


async def replace_postgresql_check(
    engine: AsyncEngine,
    *,
    table: str,
    name: str,
) -> None:
    """模拟同名恒真 CHECK，输入严格限制在固定合同清单。"""

    if (table, name) not in CHECK_TARGETS:
        raise AssertionError("unexpected CHECK target")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f'alter table "{table}" drop constraint "{name}"')
        await connection.exec_driver_sql(
            f'alter table "{table}" add constraint "{name}" check (1=1)'
        )


async def replace_postgresql_scope_check_with_bpchar(
    engine: AsyncEngine,
    *,
    table: str,
    name: str,
) -> None:
    """安装会忽略尾随空格的 bpchar 语义差异约束。"""

    if (table, name) not in {
        ("canonical_events", "ck_canonical_events_record_scope"),
        ("audit_logs", "ck_audit_logs_record_scope"),
    }:
        raise AssertionError("unexpected scope CHECK target")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f'alter table "{table}" drop constraint "{name}"')
        await connection.exec_driver_sql(
            f'alter table "{table}" add constraint "{name}" '
            "check (record_scope::bpchar in ('run'::bpchar, 'non_run'::bpchar))"
        )
