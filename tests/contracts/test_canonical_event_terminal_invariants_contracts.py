"""CanonicalEvent terminal type、flag 与 visibility 的双向不变量。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.models import CanonicalEventModel

INVALID_TERMINAL_COMBINATIONS = [
    (CanonicalEventType.DELEGATION_COMPLETED, True, "public"),
    (CanonicalEventType.DELEGATION_FAILED, True, "public"),
    (CanonicalEventType.RUN_COMPLETED, False, "public"),
    (CanonicalEventType.RUN_FAILED, True, "internal"),
]


def _event_bypassing_dto(
    *,
    event_id: str,
    event_type: CanonicalEventType,
    terminal: bool,
    visibility: str,
) -> CanonicalEvent:
    """模拟历史/内部调用绕过 Pydantic 构造，验证 sink 自身仍 fail closed。"""

    return CanonicalEvent.model_construct(
        event_id=event_id,
        tenant_id="tenant-terminal",
        run_id="stream-terminal",
        event_type=event_type,
        event_version="1.0",
        seq=0,
        payload={"status": "contract"},
        terminal=terminal,
        visibility=visibility,
        trace_id=None,
        record_scope="non_run",
    )


@pytest.mark.parametrize(
    ("event_type", "terminal", "visibility"),
    INVALID_TERMINAL_COMBINATIONS,
)
def test_dto_rejects_terminal_type_flag_visibility_mismatch(
    event_type: CanonicalEventType,
    terminal: bool,
    visibility: str,
) -> None:
    """正常 DTO 构造必须双向拒绝，不能依赖调用方记得设置三个字段。"""

    with pytest.raises(ValidationError, match="terminal"):
        CanonicalEvent(
            tenant_id="tenant-terminal",
            run_id="stream-terminal",
            event_type=event_type,
            seq=0,
            terminal=terminal,
            visibility=visibility,
            trace_id=None,
            record_scope="non_run",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "terminal", "visibility"),
    INVALID_TERMINAL_COMBINATIONS,
)
async def test_event_bus_rejects_terminal_mismatch_before_sink_queries(
    tmp_path: Path,
    event_type: CanonicalEventType,
    terminal: bool,
    visibility: str,
) -> None:
    """EventBus 必须在 seq 查询、artifact 与持久化前拒绝非法组合。"""

    class CountingSink(LocalJsonlEventSink):
        """记录 EventBus 是否在终态门禁失败前触碰底层读取接口。"""

        latest_calls = 0
        terminal_calls = 0

        async def latest_seq(self, run_id: str) -> int:
            """累计序号查询次数，再委托本地 JSONL 实现返回实际序号。"""

            self.latest_calls += 1
            return await super().latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            """累计终态查询次数，再委托本地 JSONL 实现返回实际状态。"""

            self.terminal_calls += 1
            return await super().has_terminal(run_id)

    path = tmp_path / "events.jsonl"
    sink = CountingSink(path)
    bus = EventBus(sink=sink)

    with pytest.raises(ValueError, match="terminal"):
        await bus.publish(
            tenant_id="tenant-terminal",
            run_id="stream-terminal",
            event_type=event_type,
            payload={"large": "x" * 9000},
            terminal=terminal,
            visibility=visibility,
            record_scope="non_run",
        )

    assert sink.latest_calls == 0
    assert sink.terminal_calls == 0
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "terminal", "visibility"),
    INVALID_TERMINAL_COMBINATIONS,
)
async def test_local_direct_sink_rejects_terminal_mismatch_without_file(
    tmp_path: Path,
    event_type: CanonicalEventType,
    terminal: bool,
    visibility: str,
) -> None:
    """即使 DTO 被绕过，local sink 也不能分配 seq 或创建 JSONL。"""

    path = tmp_path / f"{event_type.value}.jsonl"
    sink = LocalJsonlEventSink(path)
    event = _event_bypassing_dto(
        event_id=f"local:{event_type.value}:{terminal}:{visibility}",
        event_type=event_type,
        terminal=terminal,
        visibility=visibility,
    )

    with pytest.raises(ValueError, match="terminal"):
        await sink.write(event)

    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="terminal direct sink 合同需要真实 PostgreSQL。",
)
@pytest.mark.parametrize(
    ("event_type", "terminal", "visibility"),
    INVALID_TERMINAL_COMBINATIONS,
)
async def test_postgresql_direct_sink_rejects_terminal_mismatch_without_row(
    event_type: CanonicalEventType,
    terminal: bool,
    visibility: str,
) -> None:
    """PostgreSQL direct sink 必须在事务写入和 seq 消耗前执行同一门禁。"""

    event_id = f"postgres:{event_type.value}:{terminal}:{visibility}"
    async with isolated_database("terminal_invariant") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            event = _event_bypassing_dto(
                event_id=event_id,
                event_type=event_type,
                terminal=terminal,
                visibility=visibility,
            )
            with pytest.raises(ValueError, match="terminal"):
                await PostgreSQLEventSink(storage).write(event)
            async with storage.engine.connect() as connection:
                stored = await connection.scalar(
                    select(CanonicalEventModel.id).where(CanonicalEventModel.id == event_id)
                )
        finally:
            await storage.dispose()

    assert stored is None


def test_valid_terminal_and_nonterminal_combinations_remain_constructible() -> None:
    """双向门禁不能误伤正常 run terminal 与普通 delegation evidence。"""

    valid: list[dict[str, Any]] = [
        {
            "event_type": CanonicalEventType.RUN_COMPLETED,
            "terminal": True,
            "visibility": "public",
        },
        {
            "event_type": CanonicalEventType.DELEGATION_COMPLETED,
            "terminal": False,
            "visibility": "internal",
        },
    ]
    assert [
        CanonicalEvent(
            tenant_id="tenant-terminal",
            run_id="stream-terminal",
            seq=index,
            trace_id=None,
            record_scope="non_run",
            **values,
        ).event_type
        for index, values in enumerate(valid, start=1)
    ] == [
        CanonicalEventType.RUN_COMPLETED,
        CanonicalEventType.DELEGATION_COMPLETED,
    ]
