"""0013 canonical run trace 的 SQLite 约束与 downgrade 合同。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic import command
from tests.contracts.run_trace_migration_test_helpers import (
    migration_config,
    prepare_0012a,
    seed_identity,
    seed_run,
    sqlite_dsn,
)

from agent_harness.storage import run_migrations


def test_0013_sqlite_canonical_event_run_owner_constraint_rejects_direct_bypass(
    tmp_path: Path,
) -> None:
    """fresh schema 以联合归属约束拒绝跨租户、错 trace 与孤立 run 事件。"""

    path = tmp_path / "canonical-event-owner.db"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        seed_identity(connection, "tenant-a")
        seed_identity(connection, "tenant-b")
        with connection:
            for run_id, trace_id in (("root-a", "Trace-A"), ("root-b", "Trace-B")):
                connection.execute(
                    "insert into agent_runs("
                    "id, tenant_id, session_id, agent_id, status, trace_id, input_json, "
                    "execution_context_json) values (?, 'tenant-a', 'session-tenant-a', "
                    "'agent-a', 'created', ?, '{}', ?)",
                    (run_id, trace_id, json.dumps({"trace_id": trace_id})),
                )
                connection.execute(
                    "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) "
                    "values (?, 'tenant-a', ?)",
                    (trace_id, run_id),
                )

        def snapshot() -> tuple[str, ...]:
            return tuple(connection.iterdump())

        def insert_event(
            event_id: str,
            *,
            tenant_id: str,
            run_id: str | None,
            trace_id: str | None,
            record_scope: str,
        ) -> None:
            connection.execute(
                "insert into canonical_events("
                "id, tenant_id, run_id, stream_id, event_type, seq, terminal, visibility, "
                "trace_id, record_scope, envelope_json) values (?, ?, ?, ?, "
                "'run.started', 1, 0, 'internal', ?, ?, '{}')",
                (event_id, tenant_id, run_id, event_id, trace_id, record_scope),
            )

        with connection:
            insert_event(
                "event-valid-run",
                tenant_id="tenant-a",
                run_id="root-a",
                trace_id="Trace-A",
                record_scope="run",
            )
            insert_event(
                "event-valid-non-run",
                tenant_id="tenant-b",
                run_id=None,
                trace_id=None,
                record_scope="non_run",
            )

        for event_id, tenant_id, run_id, trace_id in (
            ("event-cross-tenant", "tenant-b", "root-a", "Trace-A"),
            ("event-wrong-trace", "tenant-a", "root-a", "Trace-B"),
            ("event-orphan-run", "tenant-a", "missing-run", "Trace-A"),
        ):
            before = snapshot()
            with pytest.raises(sqlite3.IntegrityError):
                with connection:
                    insert_event(
                        event_id,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        trace_id=trace_id,
                        record_scope="run",
                    )
            assert snapshot() == before
            assert connection.execute(
                "select count(*) from canonical_events where id=?", (event_id,)
            ).fetchone() == (0,)

        assert connection.execute(
            "select id, run_id, trace_id, record_scope from canonical_events order by id"
        ).fetchall() == [
            ("event-valid-non-run", None, None, "non_run"),
            ("event-valid-run", "root-a", "Trace-A", "run"),
        ]


def test_0013_orm_metadata_declares_canonical_event_run_owner_constraint() -> None:
    """ORM metadata 与 0013 DDL 使用相同的三列唯一键和可延迟复合外键。"""

    from agent_harness.storage.models import AgentRunModel, CanonicalEventModel

    agent_runs_table = cast(sa.Table, AgentRunModel.__table__)
    canonical_events_table = cast(sa.Table, CanonicalEventModel.__table__)
    run_unique = cast(
        sa.UniqueConstraint,
        next(
            constraint
            for constraint in agent_runs_table.constraints
            if constraint.name == "uq_agent_runs_id_tenant_trace"
        ),
    )
    event_owner = cast(
        sa.ForeignKeyConstraint,
        next(
            constraint
            for constraint in canonical_events_table.constraints
            if constraint.name == "fk_canonical_events_run_owner"
        ),
    )
    assert tuple(column.name for column in run_unique.columns) == (
        "id",
        "tenant_id",
        "trace_id",
    )
    assert tuple(element.parent.name for element in event_owner.elements) == (
        "run_id",
        "tenant_id",
        "trace_id",
    )
    assert tuple(element.target_fullname for element in event_owner.elements) == (
        "agent_runs.id",
        "agent_runs.tenant_id",
        "agent_runs.trace_id",
    )
    assert event_owner.deferrable is True
    assert event_owner.initially == "DEFERRED"


def test_0013_downgrade_requires_exact_opt_in_and_empty_evidence(tmp_path: Path) -> None:
    """只有空库与精确一次 opt-in 可回到 0012a；有 binding 时始终拒绝。"""

    for name, x_args in (
        ("missing", []),
        ("false", ["allow_empty_evidence_downgrade=false"]),
        (
            "duplicate",
            ["allow_empty_evidence_downgrade=true", "allow_empty_evidence_downgrade=true"],
        ),
    ):
        path = tmp_path / f"{name}.db"
        run_migrations(sqlite_dsn(path), "0015_agent_delegation")
        with pytest.raises(RuntimeError, match="explicit opt-in"):
            command.downgrade(
                migration_config(sqlite_dsn(path), x_args=x_args),
                "0012a_embedding_cache_tenant_scope",
            )

    empty = tmp_path / "empty.db"
    run_migrations(sqlite_dsn(empty))
    command.downgrade(
        migration_config(sqlite_dsn(empty), x_args=["allow_empty_evidence_downgrade=true"]),
        "0012a_embedding_cache_tenant_scope",
    )
    with sqlite3.connect(empty) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0012a_embedding_cache_tenant_scope",
        )
        agent_runs_sql = connection.execute(
            "select sql from sqlite_master where type='table' and name='agent_runs'"
        ).fetchone()[0]
        canonical_events_sql = connection.execute(
            "select sql from sqlite_master where type='table' and name='canonical_events'"
        ).fetchone()[0]
    assert "uq_agent_runs_id_tenant_trace" not in agent_runs_sql
    assert "fk_canonical_events_run_owner" not in canonical_events_sql

    evidence = tmp_path / "evidence.db"
    prepare_0012a(evidence)
    with sqlite3.connect(evidence) as connection:
        seed_identity(connection, "tenant-a")
        seed_run(connection, "root-a")
    run_migrations(sqlite_dsn(evidence), "0015_agent_delegation")
    with pytest.raises(RuntimeError, match="evidence exists"):
        command.downgrade(
            migration_config(sqlite_dsn(evidence), x_args=["allow_empty_evidence_downgrade=true"]),
            "0012a_embedding_cache_tenant_scope",
        )


def test_0013_audit_record_scope_database_constraint_and_downgrade(tmp_path: Path) -> None:
    """audit discriminator 必须由数据库拒绝非法值，空库降级应完整移除门禁。"""

    path = tmp_path / "audit-scope.db"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        before = connection.execute("select count(*) from audit_logs").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into audit_logs("
                "id, tenant_id, action, payload_json, record_scope"
                ") values ('audit-invalid', 'tenant-a', 'invalid', '{}', 'other')"
            )
        assert connection.execute("select count(*) from audit_logs").fetchone() == before

    empty = tmp_path / "audit-scope-empty.db"
    run_migrations(sqlite_dsn(empty), "0015_agent_delegation")
    command.downgrade(
        migration_config(sqlite_dsn(empty), x_args=["allow_empty_evidence_downgrade=true"]),
        "0012a_embedding_cache_tenant_scope",
    )
    with sqlite3.connect(empty) as connection:
        columns = {row[1] for row in connection.execute("pragma table_info('audit_logs')")}
        create_sql = connection.execute(
            "select sql from sqlite_master where type='table' and name='audit_logs'"
        ).fetchone()[0]
    assert "record_scope" not in columns
    assert "ck_audit_logs_record_scope" not in create_sql
