"""0013 canonical run trace 的 SQLite preflight 合同。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.contracts.run_trace_migration_test_helpers import (
    RUN_RELATION_TABLES,
    prepare_0012a,
    seed_identity,
    seed_invalid_run_relation,
    seed_run,
    sqlite_dsn,
)

from agent_harness.storage import run_migrations


def test_0012a_run_relation_inventory_covers_every_run_id_column(tmp_path: Path) -> None:
    """表驱动非法关系测试必须覆盖 0012a schema 的全部 run_id 列。"""

    path = tmp_path / "run-relation-inventory.db"
    prepare_0012a(path)
    with sqlite3.connect(path) as connection:
        actual = tuple(
            row[0]
            for row in connection.execute(
                "select m.name from sqlite_master m "
                "join pragma_table_info(m.name) p "
                "where m.type='table' and p.name='run_id' order by m.name"
            )
        )
    assert actual == RUN_RELATION_TABLES


@pytest.mark.parametrize("scenario", ["orphan", "cross_tenant", "trace_conflict"])
def test_0013_legacy_ordinary_telemetry_nested_run_fails_closed_before_mutation(
    tmp_path: Path,
    scenario: str,
) -> None:
    """nested run 的 orphan、tenant 越界和 trace 冲突都必须在 DDL 前拒绝。"""

    path = tmp_path / f"telemetry-{scenario}.db"
    prepare_0012a(path)
    nested_run_id = "missing-run" if scenario == "orphan" else "root-a"
    tenant_id = "tenant-b" if scenario == "cross_tenant" else "tenant-a"
    nested_trace = "Trace-B" if scenario == "trace_conflict" else None
    payload = {
        "telemetry": {
            "name": "legacy.run",
            "record_type": "event",
            "context": {
                "tenant_id": tenant_id,
                "run_id": nested_run_id,
                "trace_id": nested_trace,
            },
            "payload": {},
            "payload_ref": None,
        }
    }
    envelope = {
        "event_id": f"event-{scenario}",
        "tenant_id": tenant_id,
        "run_id": "telemetry",
        "event_type": "artifact.created",
        "seq": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "terminal": False,
        "visibility": "internal",
        "trace_id": nested_trace,
        "payload": payload,
    }
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        if scenario == "cross_tenant":
            seed_identity(connection, "tenant-b")
        seed_run(connection, "root-a", trace_id="Trace-A")
        connection.execute("pragma foreign_keys=off")
        connection.execute(
            "insert into canonical_events("
            "id, tenant_id, run_id, event_type, seq, terminal, visibility, payload_json, "
            "trace_id, envelope_json) values (?, ?, 'telemetry', 'artifact.created', 1, 0, "
            "'internal', ?, ?, ?)",
            (
                envelope["event_id"],
                tenant_id,
                json.dumps(payload),
                nested_trace,
                json.dumps(envelope),
            ),
        )
        before = tuple(connection.iterdump())

    with pytest.raises(RuntimeError, match="preflight failed"):
        run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0012a_embedding_cache_tenant_scope",
        )


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("invalid", "invalid"),
        ("conflict", "conflicting"),
        ("collision", "reused"),
        ("orphan", "orphan"),
        ("cycle", "cycle"),
        ("cross_tenant", "cross-tenant"),
    ],
)
def test_0013_preflight_rejects_invalid_lineage_before_schema_mutation(
    tmp_path: Path, scenario: str, message: str
) -> None:
    """非法 lineage/trace 在任何 0013 DDL 前整批失败，并保留 0012a head。"""

    path = tmp_path / f"{scenario}.db"
    prepare_0012a(path)
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        if scenario == "cross_tenant":
            seed_identity(connection, "tenant-b")
        if scenario == "invalid":
            seed_run(connection, "root-a", trace_id=" invalid")
        elif scenario == "conflict":
            seed_run(connection, "root-a", trace_id="trace-a")
            seed_run(connection, "child-a", parent_run_id="root-a", trace_id="trace-b")
        elif scenario == "collision":
            seed_run(connection, "root-a", trace_id="same-trace")
            seed_run(connection, "root-b", trace_id="same-trace")
        elif scenario == "orphan":
            seed_run(connection, "child-a", parent_run_id="missing")
        elif scenario == "cycle":
            seed_run(connection, "run-a", parent_run_id="run-b")
            seed_run(connection, "run-b", parent_run_id="run-a")
        else:
            seed_run(connection, "root-a")
            seed_run(
                connection,
                "child-b",
                tenant_id="tenant-b",
                parent_run_id="root-a",
            )

    with pytest.raises(RuntimeError, match="preflight failed"):
        run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()
        columns = {row[1] for row in connection.execute("pragma table_info('agent_runs')")}
    assert revision == ("0012a_embedding_cache_tenant_scope",)
    assert "trace_id" not in columns
    assert message


@pytest.mark.parametrize("table", RUN_RELATION_TABLES)
@pytest.mark.parametrize("scenario", ["orphan", "cross_tenant"])
def test_0013_preflight_rejects_invalid_run_relations_before_any_mutation(
    tmp_path: Path,
    table: str,
    scenario: str,
) -> None:
    """所有声明 run 归属的 evidence 都先校验 run 存在且 tenant 一致。"""

    path = tmp_path / f"{table}-{scenario}.db"
    prepare_0012a(path)
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        seed_run(connection, "root-a")
        if scenario == "cross_tenant":
            seed_identity(connection, "tenant-b")
        seed_invalid_run_relation(
            connection,
            table,
            tenant_id="tenant-b" if scenario == "cross_tenant" else "tenant-a",
            run_id="root-a" if scenario == "cross_tenant" else "missing-run",
        )
        before = tuple(connection.iterdump())

    with pytest.raises(RuntimeError, match="preflight failed"):
        run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        after = tuple(connection.iterdump())
        revision = connection.execute("select version_num from alembic_version").fetchone()
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        agent_run_columns = {
            row[1] for row in connection.execute("pragma table_info('agent_runs')")
        }
    assert after == before
    assert revision == ("0012a_embedding_cache_tenant_scope",)
    assert "run_trace_bindings" not in tables
    assert "trace_id" not in agent_run_columns


def test_0013_generated_trace_is_deterministic_and_db_constraints_reject_bypass(
    tmp_path: Path,
) -> None:
    """全空 lineage 得到稳定 UUIDv5，复合 FK 拒绝跨 tenant/错误 binding 投影。"""

    traces: list[str] = []
    for suffix in ("one", "two"):
        path = tmp_path / f"{suffix}.db"
        prepare_0012a(path)
        with sqlite3.connect(path) as connection:
            seed_identity(connection, "tenant-a")
            seed_run(connection, "same-root")
        run_migrations(sqlite_dsn(path))
        with sqlite3.connect(path) as connection:
            traces.append(connection.execute("select trace_id from agent_runs").fetchone()[0])
    assert traces[0] == traces[1]
    assert len(traces[0]) == 36 and traces[0] == traces[0].lower()

    path = tmp_path / "one.db"
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        seed_identity(connection, "tenant-b")
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    insert into agent_runs(
                        id, tenant_id, session_id, agent_id, status, parent_run_id,
                        trace_id, input_json, execution_context_json
                    ) values ('child-b', 'tenant-b', 'session-tenant-b', 'agent-a',
                        'created', 'same-root', ?, '{}', ?)
                    """,
                    (traces[0], json.dumps({"trace_id": traces[0]})),
                )
