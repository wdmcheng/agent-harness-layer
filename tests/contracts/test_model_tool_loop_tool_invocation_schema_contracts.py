"""0018 tool_invocations的legacy兼容与模型工具claim数据库合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate, run_migrations


def _dsn(path: Path) -> str:
    """返回隔离SQLite数据库DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def _tool_values(**overrides: object) -> dict[str, object]:
    """构造不含工具参数或结果正文的v1 claim记录。"""

    values: dict[str, object] = {
        "id": "tool-row-a",
        "tenant_id": "tenant-a",
        "agent_id": "agent-a",
        "run_id": "run-a",
        "tool_name": "search",
        "args_ref": "artifact://args",
        "result_ref": None,
        "approval_id": None,
        "arguments_hash": "a" * 64,
        "execution_state": "claimed",
        "status": "claimed",
        "metadata_json": "{}",
        "loop_id": "b" * 64,
        "turn_ordinal": 1,
        "tool_call_id": "c" * 64,
        "binding_json": "{}",
        "execution_lease_digest": "d" * 64,
        "execution_fence": 1,
        "execution_lease_expires_at": "2026-08-04 12:00:00+00:00",
        "handler_started_at": None,
        "not_started_proof_json": None,
    }
    values.update(overrides)
    return values


def _insert_tool(connection: sqlite3.Connection, **overrides: object) -> None:
    """直接插入数据库以验证DDL，而不是依赖repository预校验。"""

    values = _tool_values(**overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(
        f"insert into tool_invocations({columns}) values ({placeholders})",
        values,
    )


@pytest.mark.parametrize(
    ("execution_state", "handler_started_at", "result_ref"),
    [
        ("claimed", None, None),
        ("executing", "2026-08-04 11:59:00+00:00", None),
        ("completed", "2026-08-04 11:59:00+00:00", "artifact://result"),
        ("failed", "2026-08-04 11:59:00+00:00", "artifact://failure"),
        ("needs_review", None, None),
    ],
)
def test_model_tool_invocation_database_accepts_closed_v1_states(
    tmp_path: Path,
    execution_state: str,
    handler_started_at: str | None,
    result_ref: str | None,
) -> None:
    """五态claim的lease/fence/handler/result组合由0018 DDL接受。"""

    path = tmp_path / f"tool-{execution_state}.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        _insert_tool(
            connection,
            execution_state=execution_state,
            status=execution_state,
            handler_started_at=handler_started_at,
            result_ref=result_ref,
        )
        connection.commit()
        assert connection.execute(
            "select loop_id, turn_ordinal, tool_call_id, execution_state, execution_fence "
            "from tool_invocations"
        ).fetchone() == ("b" * 64, 1, "c" * 64, execution_state, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution_state": "unknown"},
        {"loop_id": "short"},
        {"turn_ordinal": 0},
        {"tool_call_id": "short"},
        {"binding_json": None},
        {"execution_lease_digest": "short"},
        {"execution_fence": 0},
        {"execution_lease_expires_at": None},
        {"execution_state": "claimed", "handler_started_at": "2026-08-04 11:59:00+00:00"},
        {"execution_state": "claimed", "result_ref": "artifact://early"},
        {"execution_state": "executing", "handler_started_at": None},
        {"execution_state": "completed", "result_ref": None},
        {
            "loop_id": None,
            "turn_ordinal": None,
            "tool_call_id": None,
            "binding_json": None,
            "execution_lease_digest": "d" * 64,
            "execution_fence": None,
            "execution_lease_expires_at": None,
            "execution_state": "executing",
        },
    ],
)
def test_model_tool_invocation_database_rejects_partial_or_invalid_v1_shapes(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    """部分v1、未知状态和非法lease/handler/result组合全部由数据库拒绝。"""

    path = tmp_path / "invalid-tool.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        _insert_tool(connection, **overrides)


def test_tool_call_and_approval_id_remain_independently_unique(tmp_path: Path) -> None:
    """普通与审批入口共享tool_call唯一claim，既有approval唯一键继续生效。"""

    path = tmp_path / "tool-identity.sqlite3"
    run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        _insert_tool(connection, approval_id="approval-a")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_tool(connection, id="tool-row-b", approval_id="approval-b")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_tool(
                connection,
                id="tool-row-c",
                tool_call_id="e" * 64,
                approval_id="approval-a",
            )


@pytest.mark.asyncio
async def test_legacy_tool_invocation_remains_readable_with_null_v1_fields(
    tmp_path: Path,
) -> None:
    """旧人工/审批记录不携带v1 identity，repository仍逐字段返回且不伪造claim。"""

    dsn = _dsn(tmp_path / "legacy-tool.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            created = await uow.tool_invocations.create(
                ToolInvocationCreate(
                    tenant_id="tenant-a",
                    agent_id="agent-a",
                    tool_name="search",
                    args_ref="artifact://legacy-args",
                    result_ref="artifact://legacy-result",
                    status="completed",
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            restored = await uow.tool_invocations.get(created.id)
        assert restored is not None
        assert restored.result_ref == "artifact://legacy-result"
        assert restored.loop_id is None
        assert restored.turn_ordinal is None
        assert restored.tool_call_id is None
        assert restored.binding is None
        assert restored.execution_lease_digest is None
        assert restored.execution_fence is None
        assert restored.execution_lease_expires_at is None
        assert restored.handler_started_at is None
        assert restored.not_started_proof is None
    finally:
        await storage.dispose()
