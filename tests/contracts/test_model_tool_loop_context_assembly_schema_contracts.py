"""0018 context_assemblies的legacy兼容与模型循环唯一身份合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import ContextAssemblyCreate


def _dsn(path: Path) -> str:
    """返回隔离SQLite数据库DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def _context_values(**overrides: object) -> dict[str, object]:
    """构造只保存refs与摘要、不含工具输出正文的v1 context记录。"""

    values: dict[str, object] = {
        "id": "context-row-a",
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "input_refs_json": "[]",
        "token_budget": 8,
        "trust_summary_json": "{}",
        "truncation_summary_json": "{}",
        "output_ref": "artifact://context",
        "loop_id": "a" * 64,
        "turn_ordinal": 1,
        "tool_call_id": "b" * 64,
        "input_identity_digest": "c" * 64,
        "output_digest": "d" * 64,
    }
    values.update(overrides)
    return values


def _insert_context(connection: sqlite3.Connection, **overrides: object) -> None:
    """直接写入数据库以验证唯一键和封闭shape。"""

    values = _context_values(**overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(
        f"insert into context_assemblies({columns}) values ({placeholders})",
        values,
    )


def test_model_loop_context_database_accepts_complete_v1_identity(tmp_path: Path) -> None:
    """完整loop/turn/tool/input/output摘要可写入并逐值读取。"""

    path = tmp_path / "context-v1.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        _insert_context(connection)
        connection.commit()
        assert connection.execute(
            "select loop_id, turn_ordinal, tool_call_id, input_identity_digest, output_digest "
            "from context_assemblies"
        ).fetchone() == ("a" * 64, 1, "b" * 64, "c" * 64, "d" * 64)


@pytest.mark.parametrize(
    "overrides",
    [
        {"loop_id": "short"},
        {"turn_ordinal": 0},
        {"tool_call_id": "short"},
        {"input_identity_digest": "short"},
        {"output_digest": "short"},
        {"run_id": None},
        {
            "loop_id": None,
            "turn_ordinal": None,
            "tool_call_id": None,
            "input_identity_digest": "c" * 64,
            "output_digest": None,
        },
    ],
)
def test_model_loop_context_database_rejects_partial_or_invalid_v1_identity(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    """部分v1、非正turn、短摘要和缺run都不能伪装为可恢复context。"""

    path = tmp_path / "context-invalid.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        _insert_context(connection, **overrides)


def test_model_loop_context_same_turn_is_unique_but_legacy_rows_do_not_collide(
    tmp_path: Path,
) -> None:
    """新loop同turn只有一个assembly；两个全null legacy记录仍可共存。"""

    path = tmp_path / "context-unique.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        _insert_context(connection)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_context(connection, id="context-row-b", tool_call_id="e" * 64)
        connection.rollback()
        legacy = {
            "loop_id": None,
            "turn_ordinal": None,
            "tool_call_id": None,
            "input_identity_digest": None,
            "output_digest": None,
            "run_id": None,
        }
        _insert_context(connection, id="legacy-a", **legacy)
        _insert_context(connection, id="legacy-b", **legacy)
        connection.commit()
        assert connection.execute(
            "select count(*) from context_assemblies where loop_id is null"
        ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_legacy_context_repository_returns_null_v1_identity(tmp_path: Path) -> None:
    """旧ContextAssembler记录通过公共repository读取时不生成虚假的loop identity。"""

    dsn = _dsn(tmp_path / "context-legacy.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            created = await uow.context_assemblies.create(
                ContextAssemblyCreate(
                    tenant_id="tenant-a",
                    input_refs=["source://legacy"],
                    token_budget=8,
                    output_ref="artifact://legacy-context",
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            restored = await uow.context_assemblies.get(created.id)
        assert restored is not None
        assert restored.loop_id is None
        assert restored.turn_ordinal is None
        assert restored.tool_call_id is None
        assert restored.input_identity_digest is None
        assert restored.output_digest is None
    finally:
        await storage.dispose()
