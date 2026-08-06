"""0018 model tool loop schema 的迁移与旧 binary 启动围栏合同。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from tests.contracts.run_trace_revision_hardening_postgresql_helpers import (
    postgres_database,
)

from agent_harness.storage import get_current_revision, get_head_revision, run_migrations
from agent_harness.storage.migrations import runner as migration_runner
from agent_harness.storage.migrations.runner import SchemaMigrationRequiredError

REVISION_0017 = "0017_model_route_chain_state"
REVISION_0018 = "0018_model_tool_loop_state"
MARKER_KEY = "model-tool-loop-v1"


def _dsn(path: Path) -> str:
    """返回独立SQLite文件的异步DSN，避免迁移测试共享状态。"""

    return f"sqlite+aiosqlite:///{path}"


def _sqlite_snapshot(path: Path) -> tuple[object, ...]:
    """冻结revision、目标表DDL与全行数据，证明失败门禁不会改写数据库。"""

    with sqlite3.connect(path) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()
        schema = tuple(
            connection.execute(
                "select type, name, tbl_name, sql from sqlite_master "
                "where name in ('model_tool_loops','model_tool_loop_schema_marker',"
                "'tool_invocations','context_assemblies') order by type, name"
            ).fetchall()
        )
        marker = tuple(
            connection.execute(
                "select marker_key, evidence_seen from model_tool_loop_schema_marker "
                "order by marker_key"
            ).fetchall()
        )
        counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()
            for table in (
                "model_tool_loops",
                "tool_invocations",
                "context_assemblies",
            )
        )
    return revision, schema, marker, counts


def test_0018_is_the_unique_head_and_empty_sqlite_gets_false_marker(
    tmp_path: Path,
) -> None:
    """空库升级必须落到唯一0018 head，并只创建一行false marker。"""

    path = tmp_path / "empty-0018.sqlite3"
    dsn = _dsn(path)

    assert get_head_revision() == REVISION_0018
    run_migrations(dsn)

    assert get_current_revision(dsn) == REVISION_0018
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
        }
        marker = connection.execute(
            "select marker_key, evidence_seen from model_tool_loop_schema_marker"
        ).fetchall()
    assert {"model_tool_loops", "model_tool_loop_schema_marker"} <= tables
    assert marker == [(MARKER_KEY, 0)]


def test_0018_sqlite_upgrade_preserves_legacy_tool_and_context_rows(
    tmp_path: Path,
) -> None:
    """0017 legacy记录前滚后逐值保留，不能被伪造成新loop evidence。"""

    path = tmp_path / "legacy-0018.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn, REVISION_0017)
    with sqlite3.connect(path) as connection:
        connection.execute("insert into tenants(id, display_name) values ('tenant-a', 'Tenant A')")
        connection.execute(
            "insert into tool_invocations("
            "id, tenant_id, agent_id, tool_name, args_ref, result_ref, status, metadata_json"
            ") values ('tool-legacy', 'tenant-a', 'agent-a', 'search', 'artifact://args', "
            "'artifact://result', 'completed', '{}')"
        )
        connection.execute(
            "insert into context_assemblies("
            "id, tenant_id, input_refs_json, token_budget, trust_summary_json, "
            "truncation_summary_json, output_ref"
            ") values ('context-legacy', 'tenant-a', '[]', 8, '{}', '{}', "
            "'artifact://context')"
        )
        connection.commit()
        before_tool = connection.execute(
            "select id, tenant_id, agent_id, tool_name, args_ref, result_ref, status, "
            "metadata_json from tool_invocations"
        ).fetchall()
        before_context = connection.execute(
            "select id, tenant_id, input_refs_json, token_budget, trust_summary_json, "
            "truncation_summary_json, output_ref from context_assemblies"
        ).fetchall()

    run_migrations(dsn)

    with sqlite3.connect(path) as connection:
        after_tool = connection.execute(
            "select id, tenant_id, agent_id, tool_name, args_ref, result_ref, status, "
            "metadata_json from tool_invocations"
        ).fetchall()
        after_context = connection.execute(
            "select id, tenant_id, input_refs_json, token_budget, trust_summary_json, "
            "truncation_summary_json, output_ref from context_assemblies"
        ).fetchall()
        marker = connection.execute(
            "select marker_key, evidence_seen from model_tool_loop_schema_marker"
        ).fetchall()
    assert after_tool == before_tool
    assert after_context == before_context
    assert marker == [(MARKER_KEY, 0)]


@pytest.mark.parametrize("boundary", ["repository", "worker", "model", "tool"])
@pytest.mark.parametrize("evidence_seen", [False, True])
def test_frozen_0017_binary_refuses_sqlite_0018_before_any_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    evidence_seen: bool,
) -> None:
    """旧catalog面对空0018或既有v1历史时均在四类副作用前零改写拒绝。"""

    path = tmp_path / f"old-binary-{boundary}-{evidence_seen}.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn)
    if evidence_seen:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "update model_tool_loop_schema_marker set evidence_seen = 1 where marker_key = ?",
                (MARKER_KEY,),
            )
            connection.commit()
    before = _sqlite_snapshot(path)
    effects = {"repository": 0, "worker": 0, "model": 0, "tool": 0}
    monkeypatch.setattr(migration_runner, "get_head_revision", lambda: REVISION_0017)

    def side_effect() -> None:
        effects[boundary] += 1

    with pytest.raises(
        SchemaMigrationRequiredError,
        match="database schema requires explicit migration",
    ):
        migration_runner.require_migration_head(dsn)
        side_effect()

    assert effects == {"repository": 0, "worker": 0, "model": 0, "tool": 0}
    assert _sqlite_snapshot(path) == before


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL 0018迁移合同需要AGENT_HARNESS_TEST_POSTGRES_DSN。",
)
@pytest.mark.asyncio
async def test_frozen_0017_binary_refuses_postgresql_0018_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实PostgreSQL上的旧catalog拒绝必须保留revision、marker与业务行计数。"""

    async with postgres_database("agent_harness_tool_loop_0018") as (dsn, engine):
        await asyncio.to_thread(run_migrations, dsn)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "update model_tool_loop_schema_marker set evidence_seen = true "
                    "where marker_key = :marker_key"
                ),
                {"marker_key": MARKER_KEY},
            )
        async with engine.connect() as connection:
            before = (
                (
                    await connection.execute(sa.text("select version_num from alembic_version"))
                ).scalar_one(),
                tuple(
                    (
                        await connection.execute(
                            sa.text(
                                "select marker_key, evidence_seen "
                                "from model_tool_loop_schema_marker order by marker_key"
                            )
                        )
                    ).tuples()
                ),
                (
                    await connection.execute(sa.text("select count(*) from model_tool_loops"))
                ).scalar_one(),
            )
        monkeypatch.setattr(migration_runner, "get_head_revision", lambda: REVISION_0017)
        effects = {name: 0 for name in ("repository", "worker", "model", "tool")}

        for boundary in effects:
            with pytest.raises(SchemaMigrationRequiredError):
                await asyncio.to_thread(migration_runner.require_migration_head, dsn)
                effects[boundary] += 1

        async with engine.connect() as connection:
            after = (
                (
                    await connection.execute(sa.text("select version_num from alembic_version"))
                ).scalar_one(),
                tuple(
                    (
                        await connection.execute(
                            sa.text(
                                "select marker_key, evidence_seen "
                                "from model_tool_loop_schema_marker order by marker_key"
                            )
                        )
                    ).tuples()
                ),
                (
                    await connection.execute(sa.text("select count(*) from model_tool_loops"))
                ).scalar_one(),
            )
        assert effects == {"repository": 0, "worker": 0, "model": 0, "tool": 0}
        assert after == before
