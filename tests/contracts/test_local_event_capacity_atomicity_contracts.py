"""Local JSONL 与 SQLite event capacity 的可补偿原子性合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import (
    event_bus,
    resolve_trace,
    seed_local_high_water,
    seed_run,
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.observability import TelemetryFacade, TelemetryRecord
from agent_harness.observability.context import TelemetryContext
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EvidenceOperationKind,
)


@pytest.mark.asyncio
async def test_local_ordinary_event_cannot_consume_usage_reservation(tmp_path: Path) -> None:
    """普通事件必须在写 JSONL 前保留 usage 与 terminal 的全部已预约槽位。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'ordinary-capacity.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "ordinary-capacity.jsonl"
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 3,
        )
        async with storage.uow() as uow:
            await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            before_capacity = await uow.event_capacity.snapshot(run_id)
            await uow.commit()
        before_jsonl = event_path.read_bytes()

        with pytest.raises(EventCapacityExceeded):
            await event_bus(storage=storage, event_path=event_path).publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.RUN_STARTED,
                trace_id="trace-a",
            )

        async with storage.uow() as uow:
            after_capacity = await uow.event_capacity.snapshot(run_id)
        assert event_path.read_bytes() == before_jsonl
        assert after_capacity == before_capacity
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_bound_local_sink_blocks_direct_run_telemetry_capacity_bypass(
    tmp_path: Path,
) -> None:
    """共享 sink 的 telemetry 直写也必须保留 usage 与 terminal 预约。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'telemetry-capacity.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "telemetry-capacity.jsonl"
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 3,
        )
        async with storage.uow() as uow:
            await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            before_capacity = await uow.event_capacity.snapshot(run_id)
            await uow.commit()
        sink = LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace)
        EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        telemetry = TelemetryFacade(local_sink=sink)
        before_jsonl = event_path.read_bytes()

        with pytest.raises(EventCapacityExceeded):
            await telemetry.publish_record(
                TelemetryRecord(
                    name="capacity-bypass",
                    context=TelemetryContext(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        agent_id="agent-a",
                        trace_id="trace-a",
                    ),
                )
            )

        async with storage.uow() as uow:
            after_capacity = await uow.event_capacity.snapshot(run_id)
        assert event_path.read_bytes() == before_jsonl
        assert after_capacity == before_capacity
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_local_capacity_reconciles_legacy_prefix_only_before_reservation(
    tmp_path: Path,
) -> None:
    """legacy 前缀可在新预约前接管；存在 outstanding 时必须 fail closed。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'legacy-prefix.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "legacy-prefix.jsonl"
    try:
        run_id = await seed_run(storage)
        legacy_bus = EventBus(
            sink=LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace),
            run_trace_resolver=resolve_trace,
        )
        await legacy_bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.RUN_STARTED,
            trace_id="trace-a",
        )
        capacity_bus = event_bus(storage=storage, event_path=event_path)
        await capacity_bus.reconcile_local_capacity(run_id=run_id)
        async with storage.uow() as uow:
            reconciled = await uow.event_capacity.snapshot(run_id)
            await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            await uow.commit()
        assert reconciled.highest_persisted_seq == 1

        await legacy_bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.CHECKPOINT_CREATED,
            trace_id="trace-a",
        )
        with pytest.raises(RuntimeError, match="pending evidence blocks"):
            await capacity_bus.reconcile_local_capacity(run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_bound_local_sink_keeps_synthetic_telemetry_non_run(tmp_path: Path) -> None:
    """没有 application run 容量行的 telemetry 继续作为 non-run stream 写入。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'non-run-telemetry.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "non-run-telemetry.jsonl"
    try:
        sink = LocalJsonlEventSink(event_path)
        EventBus(sink=sink, capacity_storage=storage)
        result = await TelemetryFacade(local_sink=sink).publish_record(
            TelemetryRecord(
                name="eval-score",
                context=TelemetryContext(tenant_id="tenant-a", eval_run_id="eval-a"),
            )
        )

        events = await sink.read(run_id="telemetry")
        assert result.local_status.status == "written"
        assert len(events) == 1
        assert events[0].record_scope == "non_run"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting_event_file", [False, True])
async def test_local_capacity_commit_failure_compensates_event_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting_event_file: bool,
) -> None:
    """SQLite commit 失败不得留下新行，并保留原本存在的空 JSONL。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'commit-failure.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "commit-failure.jsonl"
    artifact_root = tmp_path / "artifacts"
    try:
        if preexisting_event_file:
            event_path.touch()
        run_id = await seed_run(storage)
        async with storage.uow() as uow:
            before_capacity = await uow.event_capacity.snapshot(run_id)

        async def fail_commit(_uow: SQLAlchemyUnitOfWork) -> None:
            raise OSError("capacity commit failed")

        monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", fail_commit)
        bus = EventBus(
            sink=LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace),
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
            artifact_store=FileArtifactStore(artifact_root),
            inline_payload_bytes=1,
        )

        with pytest.raises(OSError, match="capacity commit failed"):
            await bus.publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.RUN_STARTED,
                trace_id="trace-a",
                payload={"large": "x" * 64},
            )

        async with storage.uow() as uow:
            after_capacity = await uow.event_capacity.snapshot(run_id)
        assert event_path.exists() is preexisting_event_file
        if preexisting_event_file:
            assert event_path.read_bytes() == b""
        assert list(artifact_root.glob("*.json")) == []
        assert after_capacity == before_capacity
    finally:
        await storage.dispose()
