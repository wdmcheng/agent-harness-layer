"""Model usage durable recovery、容量结算与 terminal 阻断合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    UsageEvidenceLifecycle,
    model_usage_evidence,
)
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


async def _usage_run(storage: SQLAlchemyStorage) -> str:
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
        await uow.commit()
        return run.id


@pytest.mark.asyncio
async def test_model_recovery_ignores_ordered_approval_outbox_items(tmp_path: Path) -> None:
    database = tmp_path / "mixed-recovery.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "mixed-recovery.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        evidence = model_usage_evidence(
            provider="fake",
            model="fake-basic",
            token_usage={"input_tokens": 1, "output_tokens": 1},
            latency_ms=1,
            decision={"provider_called": True},
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
        )
        bus = EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id="mixed-model",
                event_id="usage:tenant-a:mixed-model:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            await uow.commit()
        await UsageEvidenceLifecycle(
            event_bus=bus,
            evidence=evidence,
            usage_call_id="mixed-model",
        ).publish_started()
        async with storage.uow() as uow:
            await uow.evidence_outbox.persist_result(
                tenant_id="tenant-a",
                usage_call_id="mixed-model",
                result={"evidence": evidence.to_payload(), "outcome": "completed"},
            )
            await uow.evidence_outbox.stage_ordered_group(
                tenant_id="tenant-a",
                run_id=run_id,
                group_id="approval:mixed:resolution",
                items=[
                    {
                        "event_id": "approval-resolution:mixed",
                        "operation_kind": "approval_resolution",
                        "sequence_in_group": 1,
                        "reserved_event_count": 0,
                        "result": {"status": "approved"},
                    }
                ],
            )
            await uow.commit()

        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": FakeModelProvider()},
            ),
            storage=storage,
            event_bus=bus,
        )
        assert await service.recover_pending(run_id=run_id) == 1
        events = await sink.read(run_id=run_id)
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        async with storage.uow() as uow:
            pending = await uow.evidence_outbox.pending(run_id=run_id)
            pending_kinds = [item.operation_kind for item in pending]
        assert pending_kinds == ["approval_resolution"]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_final_publish_recovery_does_not_replay_provider(tmp_path: Path) -> None:
    class SpyProvider(FakeModelProvider):
        calls = 0

        def complete(self, request: ModelRequest, *, model: str):
            self.calls += 1
            return super().complete(request, model=model)

    class FailFinalOnceSink:
        manages_event_capacity = False

        def __init__(self, delegate: LocalJsonlEventSink) -> None:
            self.delegate = delegate
            self.failed = False

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self.failed:
                self.failed = True
                raise OSError("injected final write failure")
            return await self.delegate.write(event)

        async def read(self, *, run_id: str, after_seq: int = 0):
            return await self.delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            return await self.delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            return await self.delegate.has_terminal(run_id)

    database = tmp_path / "recovery.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    durable_sink = LocalJsonlEventSink(
        tmp_path / "recovery-events.jsonl",
        run_trace_resolver=resolve_trace,
    )

    provider = SpyProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    try:
        run_id = await _usage_run(storage)
        failing = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(
                sink=FailFinalOnceSink(durable_sink),
                run_trace_resolver=resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="injected final write failure"):
            await failing.complete(
                ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-recover",
            )

        recovering = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=resolve_trace),
        )
        assert await recovering.recover_pending(run_id=run_id) == 1
        assert provider.calls == 1
        events = await durable_sink.read(run_id=run_id)
        assert [item.event_type for item in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert events[-1].event_id == "usage:tenant-a:usage-recover:final"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_final_ack_loss_replays_event_only_and_settles_capacity(tmp_path: Path) -> None:
    class SpyProvider(FakeModelProvider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            self.calls += 1
            return super().complete(request, model=model)

    class LoseFinalAckOnceSink:
        manages_event_capacity = False

        def __init__(self, delegate: LocalJsonlEventSink) -> None:
            self.delegate = delegate
            self.lost = False

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            persisted = await self.delegate.write(event)
            if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self.lost:
                self.lost = True
                raise OSError("injected final acknowledgement loss")
            return persisted

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            return await self.delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            return await self.delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            return await self.delegate.has_terminal(run_id)

    database = tmp_path / "ack-loss.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    durable_sink = LocalJsonlEventSink(
        tmp_path / "ack-loss-events.jsonl",
        run_trace_resolver=resolve_trace,
    )
    provider = SpyProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    try:
        run_id = await _usage_run(storage)
        failing = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(
                sink=LoseFinalAckOnceSink(durable_sink),
                run_trace_resolver=resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="acknowledgement loss"):
            await failing.complete(
                ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-ack-loss",
            )

        recovering = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=resolve_trace),
        )
        assert await recovering.recover_pending(run_id=run_id) == 1
        assert provider.calls == 1
        events = await durable_sink.read(run_id=run_id)
        assert [item.event_type for item in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert events[-1].event_id == "usage:tenant-a:usage-ack-loss:final"
        async with storage.uow() as uow:
            assert await uow.evidence_outbox.pending(run_id=run_id) == []
            capacity = await uow.event_capacity.snapshot(run_id)
            assert capacity.highest_persisted_seq == 2
            assert capacity.outstanding_reserved_event_count == 0
            assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()


@pytest.mark.parametrize(
    "terminal_type",
    [
        CanonicalEventType.RUN_COMPLETED,
        CanonicalEventType.RUN_FAILED,
        CanonicalEventType.RUN_CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_unknown_usage_result_blocks_every_public_terminal(
    tmp_path: Path,
    terminal_type: CanonicalEventType,
) -> None:
    database = tmp_path / f"pending-{terminal_type.value}.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / f"pending-{terminal_type.value}.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        async with storage.uow() as uow:
            reserved = await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            await uow.evidence_outbox.start_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id="usage-unknown",
                event_id="usage:tenant-a:usage-unknown:final",
                reserved_event_count=reserved,
                started_evidence=model_usage_evidence(
                    provider="fake",
                    model="fake-basic",
                    token_usage={},
                    latency_ms=0,
                    decision={"provider_called": False},
                    context=UsageEvidenceContext(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        agent_id="agent-a",
                        trace_id="trace-a",
                    ),
                ).to_payload(),
            )
            await uow.commit()

        bus = EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        with pytest.raises(RuntimeError, match="pending evidence blocks terminal"):
            await bus.publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=terminal_type,
                trace_id="trace-a",
                terminal=True,
                visibility="public",
            )
        assert await sink.read(run_id=run_id) == []
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
            assert capacity.outstanding_reserved_event_count == 2
            assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()
