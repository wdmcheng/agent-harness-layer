"""Local JSONL 与 SQLite capacity 提交确认丢失合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork


@pytest.mark.asyncio
async def test_capacity_commit_ack_loss_preserves_durable_jsonl_and_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """commit 已成功但确认丢失时不得回滚 durable JSONL 或复用已占 seq。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'capacity-ack-loss.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "capacity-ack-loss.jsonl"
    try:
        run_id = await seed_run(storage)
        original_commit = SQLAlchemyUnitOfWork.commit
        acknowledgement_lost = False

        async def commit_then_lose_ack(uow: SQLAlchemyUnitOfWork) -> None:
            nonlocal acknowledgement_lost
            await original_commit(uow)
            if not acknowledgement_lost:
                acknowledgement_lost = True
                raise OSError("capacity commit acknowledgement lost")

        monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", commit_then_lose_ack)
        bus = event_bus(storage=storage, event_path=event_path)
        first = await bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.RUN_STARTED,
            trace_id="trace-a",
            event_id="event-before-ack-loss",
        )
        second = await bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.CHECKPOINT_CREATED,
            trace_id="trace-a",
            event_id="event-after-ack-loss",
        )

        events = await LocalJsonlEventSink(event_path).read(run_id=run_id)
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
        assert [first.seq, second.seq] == [1, 2]
        assert [event.event_id for event in events] == [
            "event-before-ack-loss",
            "event-after-ack-loss",
        ]
        assert capacity.highest_persisted_seq == 2
        assert capacity.outstanding_reserved_event_count == 0
        assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()
