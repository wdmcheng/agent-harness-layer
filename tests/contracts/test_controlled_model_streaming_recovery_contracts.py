"""受控模型文本流 durable outbox 补投与 provider fencing 合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import (
    stream_delta_event_id,
    stream_group_id,
)


@pytest.mark.asyncio
async def test_recovery_only_republishes_durable_stream_intent_and_never_starts_provider(
    tmp_path: Path,
) -> None:
    """`result_persisted` delta 以同 event/timestamp 补投；其余 started 不生成新 ordinal。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-recovery.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "stream-recovery.jsonl", run_trace_resolver=resolve_trace)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": FakeModelProvider()},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    usage_call_id = "2" * 64
    try:
        run_id = await seed_run(storage)
        intent = CanonicalEvent(
            event_id=stream_delta_event_id(usage_call_id, 1),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
            seq=0,
            payload={
                "correlation": {"usage_call_id": usage_call_id},
                "attempt": 1,
                "chunk_ordinal": 1,
                "text": "durable",
            },
            visibility="public",
            trace_id="trace-a",
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a", run_id=run_id, usage_call_id=usage_call_id
            )
            await uow.evidence_outbox.persist_stream_event(intent)
            await uow.commit()

        assert await service.recover_pending(run_id=run_id) == 1
        assert await service.recover_pending(run_id=run_id) == 0
        events = await sink.read(run_id=run_id)
        assert len(events) == 1
        assert events[0].event_id == intent.event_id
        assert events[0].timestamp == intent.timestamp
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            states = [item.state for item in group]
            capacity = await uow.event_capacity.snapshot(run_id)
        assert states == ["published", *("started" for _ in range(64))]
        assert capacity.highest_persisted_seq == 1
        assert capacity.outstanding_reserved_event_count == 64
    finally:
        await service.aclose()
        await storage.dispose()
