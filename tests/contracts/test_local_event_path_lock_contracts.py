"""Local JSONL 同进程多实例的共享路径锁合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
from agent_harness.storage import SQLAlchemyStorage, run_migrations


@pytest.mark.asyncio
async def test_two_event_buses_sharing_path_do_not_deadlock(tmp_path: Path) -> None:
    """同一 loop 的独立 bus/sink 必须串行进入会等待数据库的文件锁临界区。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'two-buses.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "two-buses.jsonl"
    try:
        run_id = await seed_run(storage)
        first_bus = event_bus(storage=storage, event_path=event_path)
        second_bus = event_bus(storage=storage, event_path=event_path)

        persisted = await asyncio.wait_for(
            asyncio.gather(
                first_bus.publish(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    event_type=CanonicalEventType.RUN_STARTED,
                    trace_id="trace-a",
                ),
                second_bus.publish(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    event_type=CanonicalEventType.CHECKPOINT_CREATED,
                    trace_id="trace-a",
                ),
            ),
            timeout=2,
        )

        events = await LocalJsonlEventSink(event_path).read(run_id=run_id)
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
        assert sorted(event.seq for event in persisted) == [1, 2]
        assert [event.seq for event in events] == [1, 2]
        assert capacity.highest_persisted_seq == 2
        assert capacity.outstanding_reserved_event_count == 0
        assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_reconcile_and_publish_share_same_path_lock(tmp_path: Path) -> None:
    """legacy 前缀对账与新 append 不得由独立 bus 在同一路径内互相卡死。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'reconcile-publish.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "reconcile-publish.jsonl"
    try:
        run_id = await seed_run(storage)
        reconcile_bus = event_bus(storage=storage, event_path=event_path)
        publish_bus = event_bus(storage=storage, event_path=event_path)

        _, persisted = await asyncio.wait_for(
            asyncio.gather(
                reconcile_bus.reconcile_local_capacity(run_id=run_id),
                publish_bus.publish(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    event_type=CanonicalEventType.RUN_STARTED,
                    trace_id="trace-a",
                ),
            ),
            timeout=2,
        )

        events = await LocalJsonlEventSink(event_path).read(run_id=run_id)
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
        assert persisted.seq == 1
        assert [event.seq for event in events] == [1]
        assert capacity.highest_persisted_seq == 1
        assert capacity.outstanding_reserved_event_count == 0
        assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()
