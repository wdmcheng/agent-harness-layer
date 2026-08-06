"""model_tool_loops ORM、数据库形状与乐观版本围栏合同。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.models import ModelToolLoopModel


def _dsn(path: Path) -> str:
    """返回隔离SQLite数据库的异步DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def _loop_values(**overrides: object) -> dict[str, object]:
    """构造不含业务内容、可被数据库逐字段验证的最小loop记录。"""

    values: dict[str, object] = {
        "id": "loop-row-a",
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "agent_id": "agent-a",
        "loop_id": "a" * 64,
        "request_identity_digest": "b" * 64,
        "operation_identity_digest": "c" * 64,
        "catalog_digest": "d" * 64,
        "status": "active",
        "next_turn_ordinal": 1,
        "frozen_bounds_json": "{}",
        "cumulative_usage_json": "{}",
        "state_json": "{}",
        "result_ref": None,
        "error_ref": None,
        "version": 1,
        "owner_lease_digest": "e" * 64,
        "owner_fence": 1,
        "owner_lease_expires_at": datetime(2030, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return values


def _insert_loop(connection: sqlite3.Connection, **overrides: object) -> None:
    """直接穿过数据库约束插入，避免ORM校验掩盖DDL缺口。"""

    values = _loop_values(**overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(
        f"insert into model_tool_loops({columns}) values ({placeholders})",
        values,
    )


@pytest.mark.parametrize(
    ("status", "result_ref", "error_ref"),
    [
        ("active", None, None),
        ("waiting_approval", None, None),
        ("completed", "artifact://final", None),
        ("failed", None, "error://failed"),
        ("cancelled", None, "error://cancelled"),
        ("needs_review", None, "error://needs-review"),
    ],
)
def test_model_tool_loop_database_accepts_only_the_six_valid_state_shapes(
    tmp_path: Path,
    status: str,
    result_ref: str | None,
    error_ref: str | None,
) -> None:
    """六个冻结状态各自只有一种terminal ref组合可写入。"""

    path = tmp_path / f"state-{status}.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        _insert_loop(
            connection,
            status=status,
            result_ref=result_ref,
            error_ref=error_ref,
        )
        connection.commit()
        assert connection.execute(
            "select status, next_turn_ordinal, version, result_ref, error_ref from model_tool_loops"
        ).fetchone() == (status, 1, 1, result_ref, error_ref)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "unknown"},
        {"status": "completed", "result_ref": None},
        {"status": "completed", "result_ref": "artifact://final", "error_ref": "error://x"},
        {"status": "failed", "error_ref": None},
        {"status": "active", "result_ref": "artifact://early"},
        {"agent_id": ""},
        {"loop_id": "short"},
        {"request_identity_digest": ""},
        {"operation_identity_digest": "e" * 63},
        {"catalog_digest": "f" * 65},
        {"next_turn_ordinal": 0},
        {"version": 0},
    ],
)
def test_model_tool_loop_database_rejects_invalid_identity_state_and_version(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    """未知状态、非法终态、空身份、非64位digest和非正ordinal/version全部关闭失败。"""

    path = tmp_path / "invalid-loop.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        _insert_loop(connection, **overrides)


def test_model_tool_loop_database_rejects_duplicate_tenant_loop_identity(
    tmp_path: Path,
) -> None:
    """同tenant稳定loop identity只能有一行。"""

    path = tmp_path / "duplicate-loop.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        _insert_loop(connection)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_loop(connection, id="loop-row-b")


@pytest.mark.asyncio
async def test_model_tool_loop_orm_rejects_terminal_regression_and_stale_version(
    tmp_path: Path,
) -> None:
    """ORM拒绝终态倒退，两个writer基于同一version时只允许首个提交。"""

    path = tmp_path / "loop-version.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn)
    with sqlite3.connect(path) as connection:
        _insert_loop(connection)
        connection.commit()

    storage = SQLAlchemyStorage(dsn)
    try:
        async with storage.uow() as first, storage.uow() as stale:
            first_row = await first.session.get(ModelToolLoopModel, "loop-row-a")
            stale_row = await stale.session.get(ModelToolLoopModel, "loop-row-a")
            assert first_row is not None
            assert stale_row is not None
            first_row.status = "waiting_approval"
            await first.commit()
            assert first_row.version == 2
            stale_row.status = "failed"
            stale_row.error_ref = "error://stale"
            with pytest.raises(StaleDataError):
                await stale.commit()

        async with storage.uow() as terminal:
            terminal_row = await terminal.session.get(ModelToolLoopModel, "loop-row-a")
            assert terminal_row is not None
            terminal_row.status = "active"
            await terminal.commit()

        async with storage.uow() as terminal:
            terminal_row = await terminal.session.get(ModelToolLoopModel, "loop-row-a")
            assert terminal_row is not None
            terminal_row.status = "completed"
            terminal_row.result_ref = "artifact://final"
            await terminal.commit()

        async with storage.uow() as regressing:
            terminal_row = await regressing.session.get(ModelToolLoopModel, "loop-row-a")
            assert terminal_row is not None
            terminal_row.status = "active"
            terminal_row.result_ref = None
            with pytest.raises(ValueError, match="model tool loop status transition is invalid"):
                await regressing.commit()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_tool_loop_orm_surfaces_database_shape_conflicts(
    tmp_path: Path,
) -> None:
    """ORM不能把数据库拒绝包装成成功或静默修正调用方字段。"""

    dsn = _dsn(tmp_path / "orm-invalid.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        async with storage.uow() as uow:
            uow.session.add(
                ModelToolLoopModel(
                    **_loop_values(status="unknown"),
                )
            )
            with pytest.raises(IntegrityError):
                await uow.commit()
    finally:
        await storage.dispose()
