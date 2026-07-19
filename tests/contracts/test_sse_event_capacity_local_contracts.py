"""Local/SQLite 序号上限与 terminal-last 合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update
from tests.contracts.model_usage_capacity_test_helpers import (
    event_bus,
    seed_local_high_water,
    seed_run,
)

from agent_harness.events import CanonicalEvent, CanonicalEventType, canonical_event_bytes
from agent_harness.local_state import register_local_state_file
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventSequenceStateInvalid,
    EvidenceOperationKind,
)
from agent_harness.storage.models import RunEventCapacityModel


@pytest.mark.asyncio
async def test_local_seq_max_non_terminal_is_state_invalid_and_has_no_partial_write(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'seq-max-invalid.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "seq-max-invalid.jsonl"
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 1,
        )
        before_jsonl = event_path.read_bytes()
        async with storage.uow() as uow:
            before_capacity = await uow.event_capacity.snapshot(run_id)

        with pytest.raises(EventSequenceStateInvalid) as rejected:
            async with storage.uow() as uow:
                await uow.event_capacity.reconcile_local_prefix(
                    run_id=run_id,
                    highest_persisted_seq=MAX_EVENT_SEQ,
                )

        assert rejected.value.code == "event.sequence_state_invalid"
        assert event_path.read_bytes() == before_jsonl
        async with storage.uow() as uow:
            assert await uow.event_capacity.snapshot(run_id) == before_capacity
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_local_terminal_consumes_reserved_seq_max_as_last_event(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'terminal-max.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "terminal-max.jsonl"
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 1,
        )
        bus = event_bus(storage=storage, event_path=event_path)

        terminal = await bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.RUN_COMPLETED,
            terminal=True,
            visibility="public",
            trace_id="trace-a",
        )

        assert terminal.seq == MAX_EVENT_SEQ
        assert terminal == await bus.terminal_event(run_id)
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
        assert capacity.highest_persisted_seq == MAX_EVENT_SEQ
        assert capacity.terminal_reservation == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_local_invalid_high_water_shape_is_not_reported_as_exhaustion(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'invalid-high-water.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        run_id = await seed_run(storage)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(
                    highest_persisted_seq=MAX_EVENT_SEQ,
                    terminal_reservation=0,
                )
            )
            await uow.commit()

        async with storage.uow() as uow:
            with pytest.raises(EventSequenceStateInvalid) as rejected:
                await uow.event_capacity.reserve(
                    run_id=run_id,
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                )
        assert rejected.value.code == "event.sequence_state_invalid"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_local_legacy_seq_max_non_terminal_prefix_is_state_invalid(
    tmp_path: Path,
) -> None:
    """未对齐账本的 direct-write 最大 seq 必须按损坏状态拒绝。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'legacy-seq-max.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "legacy-seq-max.jsonl"
    try:
        run_id = await seed_run(storage)
        legacy = CanonicalEvent(
            event_id="legacy-seq-max-non-terminal",
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.RUN_STARTED,
            seq=MAX_EVENT_SEQ,
            trace_id="trace-a",
        )
        register_local_state_file(event_path, kind="events")
        event_path.write_bytes(canonical_event_bytes(legacy) + b"\n")
        before_jsonl = event_path.read_bytes()
        async with storage.uow() as uow:
            before_capacity = await uow.event_capacity.snapshot(run_id)

        with pytest.raises(EventSequenceStateInvalid) as rejected:
            async with storage.uow() as uow:
                await uow.event_capacity.reconcile_local_prefix(
                    run_id=run_id,
                    highest_persisted_seq=MAX_EVENT_SEQ,
                )

        assert rejected.value.code == "event.sequence_state_invalid"
        assert event_path.read_bytes() == before_jsonl
        async with storage.uow() as uow:
            assert await uow.event_capacity.snapshot(run_id) == before_capacity
    finally:
        await storage.dispose()
