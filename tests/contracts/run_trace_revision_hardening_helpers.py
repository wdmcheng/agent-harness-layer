"""0013a run trace 事件硬化合同共享的 SQLite 迁移 fixture。"""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from agent_harness.storage.migrations.runner import alembic_config

REVISION_0013 = "0013_run_trace_correlation"
REVISION_0013A = "0013a_run_trace_event_hardening"
CHECK_TARGETS = (
    ("canonical_events", "ck_canonical_events_record_scope"),
    ("canonical_events", "ck_canonical_events_run_ownership"),
    ("canonical_events", "ck_canonical_events_non_run_ownership"),
    ("audit_logs", "ck_audit_logs_record_scope"),
)


def sqlite_dsn(path: Path) -> str:
    """生成每个 fixture 专属的异步 SQLite DSN，保持 revision 回退试验之间完全隔离。"""

    return f"sqlite+aiosqlite:///{path}"


def migration_config(dsn: str, *, x_args: list[str] | None = None) -> Config:
    """创建可注入 Alembic 运行参数的配置，供硬化迁移的精确升级与降级断言使用。"""

    config = alembic_config(dsn)
    config.cmd_opts = Namespace(x=x_args or [])
    return config


def seed_legacy_event_rows(path: Path) -> None:
    """写入同时含 run/non-run 事件的历史数据集，覆盖 0013a 必须兼容的真实旧形状。"""

    with sqlite3.connect(path) as connection:
        connection.execute("insert into tenants(id, display_name) values ('tenant-a', 'A')")
        connection.execute(
            "insert into sessions(id, tenant_id, user_id, metadata_json) "
            "values ('session-a', 'tenant-a', 'user-a', '{}')"
        )
        connection.execute(
            "insert into agent_runs(id, tenant_id, session_id, agent_id, status, trace_id, "
            "input_json, execution_context_json) values ('root-a', 'tenant-a', 'session-a', "
            "'agent-a', 'created', 'Trace-A', '{}', '{\"trace_id\":\"Trace-A\"}')"
        )
        connection.execute(
            "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) "
            "values ('Trace-A', 'tenant-a', 'root-a')"
        )
        for event_id, seq, scope in (
            ("legacy-run", 1, "run"),
            ("legacy-non-run", 2, "non_run"),
        ):
            envelope = json.dumps(
                {
                    "event_id": event_id,
                    "tenant_id": "tenant-a",
                    "run_id": "root-a",
                    "event_type": "run.started",
                    "seq": seq,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "terminal": False,
                    "visibility": "internal",
                    "trace_id": "Trace-A",
                    "record_scope": scope,
                    "payload": {},
                }
            )
            connection.execute(
                "insert into canonical_events(id, tenant_id, run_id, event_type, seq, "
                "terminal, visibility, trace_id, envelope_json, record_scope) values "
                "(?, 'tenant-a', 'root-a', 'run.started', ?, 0, 'internal', "
                "'Trace-A', ?, ?)",
                (event_id, seq, envelope, scope),
            )
        connection.execute(
            "insert into audit_logs(id, tenant_id, action, payload_json, record_scope) "
            "values ('audit-a', 'tenant-a', 'run.started', '{}', 'run')"
        )


def simulate_legacy_sqlite_0013(path: Path) -> None:
    """把当前 0013 空 schema 还原成现场旧 0013 的完整事件签名。"""

    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        migration = MigrationContext.configure(connection)
        operations = Operations(migration)
        with connection.begin():
            operations.drop_index(
                "ix_canonical_events_stream_id",
                table_name="canonical_events",
            )
            with operations.batch_alter_table(
                "canonical_events",
                recreate="always",
            ) as batch_op:
                batch_op.drop_constraint("fk_canonical_events_run_owner", type_="foreignkey")
                batch_op.drop_constraint(
                    "ck_canonical_events_non_run_ownership",
                    type_="check",
                )
                batch_op.drop_constraint("ck_canonical_events_run_ownership", type_="check")
                batch_op.drop_constraint("ck_canonical_events_record_scope", type_="check")
                batch_op.drop_constraint(
                    "uq_canonical_events_tenant_stream_seq",
                    type_="unique",
                )
                batch_op.alter_column(
                    "trace_id",
                    existing_type=sa.String(length=128),
                    nullable=False,
                )
                batch_op.drop_column("stream_id")
                batch_op.alter_column(
                    "run_id",
                    existing_type=sa.String(length=36),
                    nullable=False,
                )
                batch_op.create_unique_constraint(
                    "uq_canonical_events_run_seq",
                    ["run_id", "seq"],
                )
            with operations.batch_alter_table("audit_logs", recreate="always") as batch_op:
                batch_op.drop_constraint("ck_audit_logs_record_scope", type_="check")
            with operations.batch_alter_table("agent_runs", recreate="always") as batch_op:
                batch_op.drop_constraint("uq_agent_runs_id_tenant_trace", type_="unique")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    engine.dispose()


def sqlite_snapshot(path: Path) -> tuple[object, ...]:
    """提取 schema、关键事件、run 与 revision 的轻量快照，用于比较迁移副作用是否超出预期。"""

    with sqlite3.connect(path) as connection:
        schema = tuple(
            connection.execute(
                "select type, name, tbl_name, sql from sqlite_master "
                "where name not like 'sqlite_%' order by type, name"
            )
        )
        events = tuple(
            connection.execute(
                "select id, tenant_id, run_id, event_type, seq, trace_id, record_scope "
                "from canonical_events order by id"
            )
        )
        runs = tuple(connection.execute("select id, tenant_id, trace_id from agent_runs"))
        revision = connection.execute("select version_num from alembic_version").fetchone()
    return schema, events, runs, revision


def sqlite_full_snapshot(path: Path) -> tuple[object, ...]:
    """捕获 CHECK 迁移可能影响的完整表 DDL、全行数据与 revision。"""

    with sqlite3.connect(path) as connection:
        schema = tuple(
            connection.execute(
                "select type, name, tbl_name, sql from sqlite_master "
                "where name not like 'sqlite_%' order by type, name"
            )
        )
        data = tuple(
            (
                table,
                tuple(connection.execute(f'select * from "{table}" order by rowid')),
            )
            for table in ("agent_runs", "audit_logs", "canonical_events", "run_trace_bindings")
        )
        revision = connection.execute("select version_num from alembic_version").fetchone()
    return schema, data, revision


def replace_sqlite_check(
    path: Path,
    *,
    table: str,
    name: str,
    expression: str = "1=1",
) -> None:
    """替换固定目标 CHECK；只用于验证 0013a 在 stamp 前拒绝。"""

    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        migration = MigrationContext.configure(connection)
        operations = Operations(migration)
        with connection.begin():
            with operations.batch_alter_table(table, recreate="always") as batch_op:
                batch_op.drop_constraint(name, type_="check")
                batch_op.create_check_constraint(name, expression)
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    engine.dispose()
