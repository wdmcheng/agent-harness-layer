"""0013a 已发布 shape 的 SQLite 前滚与运行前门禁合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from tests.contracts.run_trace_revision_hardening_helpers import (
    CHECK_TARGETS,
    REVISION_0013,
    REVISION_0013A,
    migration_config,
    replace_sqlite_check,
    seed_legacy_event_rows,
    simulate_legacy_sqlite_0013,
    sqlite_dsn,
    sqlite_full_snapshot,
    sqlite_snapshot,
)

from agent_harness.storage import run_migrations
from agent_harness.storage.migrations.runner import (
    SchemaMigrationRequiredError,
    alembic_config,
    get_current_revision,
    require_migration_head,
)


def test_old_sqlite_0013_is_rejected_before_side_effect_then_hardened(tmp_path: Path) -> None:
    """旧同名 revision 不再假 PASS；前滚后 legacy scope 与数据库门禁成立。"""

    path = tmp_path / "legacy-0013.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn, REVISION_0013)
    simulate_legacy_sqlite_0013(path)
    seed_legacy_event_rows(path)
    before = sqlite_snapshot(path)

    with pytest.raises(SchemaMigrationRequiredError):
        require_migration_head(dsn)
    assert sqlite_snapshot(path) == before

    run_migrations(dsn)
    assert require_migration_head(dsn) == REVISION_0013A
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "select id, run_id, stream_id, trace_id, record_scope from canonical_events order by id"
        ).fetchall()
        constraints = connection.execute(
            "select sql from sqlite_master where type='table' "
            "and name in ('agent_runs', 'audit_logs', 'canonical_events') order by name"
        ).fetchall()
    assert rows == [
        ("legacy-non-run", None, "root-a", "Trace-A", "non_run"),
        ("legacy-run", "root-a", "root-a", "Trace-A", "run"),
    ]
    ddl = "\n".join(row[0] for row in constraints)
    for name in (
        "uq_agent_runs_id_tenant_trace",
        "ck_audit_logs_record_scope",
        "ck_canonical_events_record_scope",
        "ck_canonical_events_run_ownership",
        "ck_canonical_events_non_run_ownership",
        "fk_canonical_events_run_owner",
        "uq_canonical_events_tenant_stream_seq",
    ):
        assert name in ddl

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into canonical_events(id, tenant_id, run_id, stream_id, event_type, "
                "seq, terminal, visibility, trace_id, record_scope) values "
                "('bad-owner', 'tenant-a', 'root-a', 'bad-owner', 'run.started', 1, 0, "
                "'internal', 'Trace-B', 'run')"
            )
            connection.commit()
        connection.rollback()
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into audit_logs(id, tenant_id, action, payload_json, record_scope) "
                "values ('bad-audit', 'tenant-a', 'bad', '{}', 'other')"
            )

    before_downgrade = sqlite_snapshot(path)
    command.downgrade(migration_config(dsn), REVISION_0013)
    after_stamp_downgrade = sqlite_snapshot(path)
    assert after_stamp_downgrade[:3] == before_downgrade[:3]
    assert after_stamp_downgrade[3] == (REVISION_0013,)

    with pytest.raises(RuntimeError, match="0013 downgrade refused: explicit opt-in"):
        command.downgrade(migration_config(dsn), "0012a_embedding_cache_tenant_scope")
    with pytest.raises(RuntimeError, match="0013 downgrade refused: canonical trace evidence"):
        command.downgrade(
            migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
            "0012a_embedding_cache_tenant_scope",
        )
    assert sqlite_snapshot(path) == after_stamp_downgrade


def test_current_0013_shape_is_verified_noop_and_downgrade_preserves_schema(
    tmp_path: Path,
) -> None:
    """fresh 0013 已是最终 shape 时 0013a 只验证；stamp 回退不删除硬化 DDL。"""

    path = tmp_path / "fresh-0013.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn, REVISION_0013)
    before = sqlite_snapshot(path)
    run_migrations(dsn)
    after = sqlite_snapshot(path)
    assert after[:3] == before[:3]
    assert after[3] == (REVISION_0013A,)

    command.downgrade(migration_config(dsn), REVISION_0013)
    assert get_current_revision(dsn) == REVISION_0013
    downgraded = sqlite_snapshot(path)
    assert downgraded[:3] == before[:3]
    with pytest.raises(SchemaMigrationRequiredError):
        require_migration_head(dsn)
    with pytest.raises(RuntimeError, match="0013 downgrade refused: explicit opt-in"):
        command.downgrade(alembic_config(dsn), "0012a_embedding_cache_tenant_scope")


def test_partial_0013_shape_fails_before_any_sqlite_mutation(tmp_path: Path) -> None:
    """旧/最终签名之外的混合 shape 不得被迁移器猜测修补。"""

    path = tmp_path / "partial-0013.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn, REVISION_0013)
    simulate_legacy_sqlite_0013(path)
    with sqlite3.connect(path) as connection:
        connection.execute("alter table canonical_events add column stream_id varchar(128)")
    before = sqlite_snapshot(path)

    with pytest.raises(RuntimeError, match="incompatible or partial 0013 schema shape"):
        run_migrations(dsn)
    assert sqlite_snapshot(path) == before


@pytest.mark.parametrize(("table", "name"), CHECK_TARGETS)
def test_same_named_weakened_sqlite_check_fails_without_side_effects(
    tmp_path: Path,
    table: str,
    name: str,
) -> None:
    """四个目标 CHECK 任一被同名恒真替换，都不能被 0013a stamp 成 head。"""

    path = tmp_path / f"weakened-{name}.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn, REVISION_0013)
    replace_sqlite_check(path, table=table, name=name)
    before = sqlite_full_snapshot(path)

    with pytest.raises(RuntimeError, match="incompatible or partial 0013 schema shape"):
        run_migrations(dsn)

    assert sqlite_full_snapshot(path) == before


def test_deep_parentheses_sqlite_check_fails_closed_without_side_effects(
    tmp_path: Path,
) -> None:
    """真实 SQLite 深层括号表达式越过受控上限时稳定拒绝且零变更。"""

    path = tmp_path / "deep-parentheses.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn, REVISION_0013)
    expression = "(" * 65 + "record_scope IN ('run', 'non_run')" + ")" * 65
    replace_sqlite_check(
        path,
        table="canonical_events",
        name="ck_canonical_events_record_scope",
        expression=expression,
    )
    before = sqlite_full_snapshot(path)

    with pytest.raises(RuntimeError, match="incompatible or partial 0013 schema shape"):
        run_migrations(dsn)

    assert sqlite_full_snapshot(path) == before
