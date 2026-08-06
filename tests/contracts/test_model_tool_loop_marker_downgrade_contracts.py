"""0018 evidence marker的同UoW单调写入与downgrade拒绝合同。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.storage import (
    ModelToolLoopCreate,
    SQLAlchemyStorage,
    ToolInvocationCreate,
    get_current_revision,
    run_migrations,
)
from agent_harness.storage.models import ModelToolLoopModel, ModelToolLoopSchemaMarkerModel
from agent_harness.storage.repositories import ContextAssemblyCreate, RunCreate, SessionCreate

MARKER_KEY = "model-tool-loop-v1"
REVISION_0017 = "0017_model_route_chain_state"
REVISION_0018 = "0018_model_tool_loop_state"


def _dsn(path: Path) -> str:
    """返回隔离SQLite数据库DSN。"""

    return f"sqlite+aiosqlite:///{path}"


async def _create_run(storage: SQLAlchemyStorage) -> str:
    """创建带canonical trace的最小run，满足v1 tool/context外键。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.create(
            SessionCreate(tenant_id="tenant-a", user_id="user-a", agent_id="agent-a")
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-a",
                trace_id="trace-marker-a",
            )
        )
        await uow.commit()
    return run.id


async def _create_active_loop(storage: SQLAlchemyStorage, run_id: str) -> None:
    """通过公共仓储建立工具与上下文写入必须依附的active loop。"""

    async with storage.uow() as uow:
        await uow.model_tool_loops.create(
            ModelToolLoopCreate(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                loop_id="b" * 64,
                request_identity_digest="2" * 64,
                operation_identity_digest="3" * 64,
                catalog_digest="4" * 64,
                **initial_model_tool_loop_snapshot(),
                owner_lease_digest="5" * 64,
                owner_fence=1,
                owner_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await uow.commit()


def _tool_create(run_id: str) -> ToolInvocationCreate:
    """构造claimed v1工具记录，所有摘要均为固定测试值。"""

    return ToolInvocationCreate(
        tenant_id="tenant-a",
        agent_id="agent-a",
        run_id=run_id,
        tool_name="search",
        args_ref="artifact://args",
        arguments_hash="a" * 64,
        execution_state="claimed",
        status="claimed",
        loop_id="b" * 64,
        turn_ordinal=1,
        tool_call_id="c" * 64,
        binding={"schema_version": "tool-binding-v1"},
        execution_lease_digest="d" * 64,
        execution_fence=1,
        execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_tool_or_context_v1_evidence_commits_in_the_same_uow(
    tmp_path: Path,
) -> None:
    """工具或上下文必须依附active loop，未提交证据回滚且marker保持单调。"""

    dsn = _dsn(tmp_path / "marker-uow.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _create_run(storage)
        await _create_active_loop(storage, run_id)
        async with storage.uow() as uow:
            await uow.tool_invocations.create(_tool_create(run_id))

        async with storage.uow() as uow:
            marker = await uow.model_tool_loop_marker.get()
            tool = await uow.tool_invocations.get_by_tool_call_id("c" * 64)
        assert marker.evidence_seen is True
        assert tool is None

        async with storage.uow() as uow:
            tool = await uow.tool_invocations.create(_tool_create(run_id))
            await uow.commit()
        async with storage.uow() as uow:
            marker = await uow.model_tool_loop_marker.get()
            restored = await uow.tool_invocations.get(tool.id)
        assert marker.evidence_seen is True
        assert restored is not None

        async with storage.uow() as uow:
            context = await uow.context_assemblies.create(
                ContextAssemblyCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    input_refs=["artifact://tool-result"],
                    token_budget=8,
                    output_ref="artifact://context",
                    loop_id="b" * 64,
                    turn_ordinal=2,
                    tool_call_id="e" * 64,
                    input_identity_digest="f" * 64,
                    output_digest="1" * 64,
                )
            )
            await uow.commit()
        assert context.loop_id == "b" * 64
        async with storage.uow() as uow:
            assert (await uow.model_tool_loop_marker.get()).evidence_seen is True
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_first_loop_row_marks_and_rolls_back_in_the_same_uow(tmp_path: Path) -> None:
    """ORM loop INSERT自身拥有marker提升；未提交时loop与marker提升一起回滚。"""

    dsn = _dsn(tmp_path / "marker-loop-uow.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _create_run(storage)

        def loop_row(row_id: str) -> ModelToolLoopModel:
            return ModelToolLoopModel(
                id=row_id,
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                loop_id="2" * 64,
                request_identity_digest="3" * 64,
                operation_identity_digest="4" * 64,
                catalog_digest="5" * 64,
                status="active",
                next_turn_ordinal=1,
                frozen_bounds_json={"schema_version": "model-tool-loop-bounds-v1"},
                cumulative_usage_json={"turns_completed": 0},
                state_json={"schema_version": "model-tool-loop-state-v1"},
                version=1,
                owner_lease_digest="6" * 64,
                owner_fence=1,
                owner_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

        async with storage.uow() as uow:
            uow.session.add(loop_row("loop-row-rollback"))
            await uow.session.flush()

        async with storage.uow() as uow:
            marker = await uow.model_tool_loop_marker.get()
            rolled_back = await uow.session.get(ModelToolLoopModel, "loop-row-rollback")
        assert marker.evidence_seen is False
        assert rolled_back is None

        async with storage.uow() as uow:
            uow.session.add(loop_row("loop-row-commit"))
            await uow.commit()
        async with storage.uow() as uow:
            marker = await uow.model_tool_loop_marker.get()
            committed = await uow.session.get(ModelToolLoopModel, "loop-row-commit")
        assert marker.evidence_seen is True
        assert committed is not None
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_marker_rejects_true_to_false_and_delete_through_orm(tmp_path: Path) -> None:
    """支持的ORM维护入口不能清零或删除marker。"""

    dsn = _dsn(tmp_path / "marker-immutable.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        async with storage.uow() as uow:
            await uow.model_tool_loop_marker.mark_evidence_seen()
            await uow.commit()

        async with storage.uow() as uow:
            marker = await uow.session.get(ModelToolLoopSchemaMarkerModel, MARKER_KEY)
            assert marker is not None
            marker.evidence_seen = False
            with pytest.raises(ValueError, match="model tool loop schema marker is monotonic"):
                await uow.commit()

        async with storage.uow() as uow:
            marker = await uow.session.get(ModelToolLoopSchemaMarkerModel, MARKER_KEY)
            assert marker is not None
            await uow.session.delete(marker)
            with pytest.raises(ValueError, match="model tool loop schema marker cannot be deleted"):
                await uow.commit()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_deleted_business_evidence_cannot_clear_downgrade_history(tmp_path: Path) -> None:
    """业务行删除后marker仍拒绝0018到0017，revision和marker保持不变。"""

    path = tmp_path / "marker-history.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _create_run(storage)
        await _create_active_loop(storage, run_id)
        async with storage.uow() as uow:
            await uow.tool_invocations.create(_tool_create(run_id))
            await uow.commit()
    finally:
        await storage.dispose()

    with sqlite3.connect(path) as connection:
        connection.execute("delete from tool_invocations where tool_call_id is not null")
        connection.commit()
        assert connection.execute(
            "select evidence_seen from model_tool_loop_schema_marker where marker_key = ?",
            (MARKER_KEY,),
        ).fetchone() == (1,)

    with pytest.raises(RuntimeError, match="^storage.model_tool_loop_evidence_present$"):
        await asyncio.to_thread(command.downgrade, migration_config(dsn), REVISION_0017)
    assert await asyncio.to_thread(get_current_revision, dsn) == REVISION_0018


def test_scan_rejects_v1_evidence_even_if_marker_is_false(tmp_path: Path) -> None:
    """缺陷或外部写入遗漏marker时，downgrade仍扫描v1 identity并失败关闭。"""

    path = tmp_path / "marker-scan.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "insert into context_assemblies("
            "id, tenant_id, run_id, input_refs_json, token_budget, trust_summary_json, "
            "truncation_summary_json, output_ref, loop_id, turn_ordinal, tool_call_id, "
            "input_identity_digest, output_digest) values ("
            "'context-v1', 'tenant-a', 'run-a', '[]', 8, '{}', '{}', "
            "'artifact://context', ?, 1, ?, ?, ?)",
            ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="^storage.model_tool_loop_evidence_present$"):
        command.downgrade(migration_config(dsn), REVISION_0017)
    assert get_current_revision(dsn) == REVISION_0018


def test_false_marker_and_empty_scan_is_the_only_supported_downgrade(tmp_path: Path) -> None:
    """只有从未产生v1 evidence的0018可回到0017，且全部新增schema被移除。"""

    path = tmp_path / "marker-empty.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn)

    command.downgrade(migration_config(dsn), REVISION_0017)

    assert get_current_revision(dsn) == REVISION_0017
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
        }
        tool_columns = {row[1] for row in connection.execute("pragma table_info(tool_invocations)")}
        context_columns = {
            row[1] for row in connection.execute("pragma table_info(context_assemblies)")
        }
    assert "model_tool_loops" not in tables
    assert "model_tool_loop_schema_marker" not in tables
    assert "tool_call_id" not in tool_columns
    assert "tool_call_id" not in context_columns
