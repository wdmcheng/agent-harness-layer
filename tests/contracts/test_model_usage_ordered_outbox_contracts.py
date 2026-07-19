"""Approval resolution 与 terminal ordered outbox 合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)


def sqlite_dsn(path: Path) -> str:
    """为有序 outbox 合同创建独立 SQLite 异步 DSN。"""

    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_approval_resolution_and_terminal_share_ordered_outbox_group(tmp_path: Path) -> None:
    """验证审批解析事件与 run 终态事件作为一个有序组持久化，避免发布重排。"""

    path = tmp_path / "approval-outbox.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            reserved = await uow.event_capacity.reserve(
                run_id=run.id,
                operation_kind=EvidenceOperationKind.APPROVAL_RESOLUTION,
            )
            await uow.evidence_outbox.stage_ordered_group(
                tenant_id="tenant-a",
                run_id=run.id,
                group_id="approval:approval-a:resolution",
                items=[
                    {
                        "event_id": "approval-resolution:approval-a",
                        "operation_kind": "approval_resolution",
                        "sequence_in_group": 1,
                        "reserved_event_count": reserved,
                    },
                    {
                        "event_id": f"run-terminal:{run.id}",
                        "operation_kind": "run_terminal",
                        "sequence_in_group": 2,
                        "reserved_event_count": 0,
                    },
                ],
            )
            await uow.commit()

        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(
                group_id="approval:approval-a:resolution"
            )
            assert [item.event_id for item in group] == [
                "approval-resolution:approval-a",
                f"run-terminal:{run.id}",
            ]
            assert [item.sequence_in_group for item in group] == [1, 2]
            assert [item.state for item in group] == ["result_persisted", "result_persisted"]
    finally:
        await storage.dispose()
