"""稳定 usage_call_id 的连续与并发重试合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations


async def _seed_run(storage: SQLAlchemyStorage) -> str:
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


def _context(run_id: str) -> UsageEvidenceContext:
    return UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


async def _resolve_trace(**_: object) -> str:
    return "trace-a"


class CountingModelProvider(FakeModelProvider):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        self.calls += 1
        return super().complete(request, model=model)


class CountingEmbeddingProvider:
    provider = "counting-embedding"
    model = "embedding-model"

    def __init__(self, *, release: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = release

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return EmbeddingResponse(
            provider=self.provider,
            model=self.model,
            vector_ref="embedding://counting/result",
            vector=[0.25],
            cache=EmbeddingCacheInfo(
                hit=False,
                input_hash="hash-a",
                vector_ref="embedding://counting/result",
            ),
            latency_ms=1,
        )


class FailFinalOnceSink:
    """让 provider 结果先落 outbox，再在最终 event 写前制造可恢复中断。"""

    manages_event_capacity = False

    def __init__(self, delegate: LocalJsonlEventSink) -> None:
        self.delegate = delegate
        self.failed = False

    async def write(self, event: CanonicalEvent) -> CanonicalEvent:
        if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self.failed:
            self.failed = True
            raise OSError("injected final write failure")
        return await self.delegate.write(event)

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        return await self.delegate.read(run_id=run_id, after_seq=after_seq)

    async def latest_seq(self, run_id: str) -> int:
        return await self.delegate.latest_seq(run_id)

    async def has_terminal(self, run_id: str) -> bool:
        return await self.delegate.has_terminal(run_id)


async def _assert_settled_once(
    *, storage: SQLAlchemyStorage, sink: LocalJsonlEventSink, run_id: str
) -> None:
    events = await sink.read(run_id=run_id)
    assert [event.event_type.value for event in events] == [
        "model.request.started",
        "model.usage.updated",
    ]
    async with storage.uow() as uow:
        capacity = await uow.event_capacity.snapshot(run_id)
        outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
        outbox_states = [item.state for item in outbox]
    assert outbox_states == ["published"]
    assert capacity.outstanding_reserved_event_count == 0
    assert capacity.highest_persisted_seq == 2
    assert capacity.terminal_reservation == 1


__all__ = [
    "CanonicalEvent",
    "CanonicalEventType",
    "CountingEmbeddingProvider",
    "CountingModelProvider",
    "EmbeddingCacheInfo",
    "EmbeddingInvocationService",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "EventBus",
    "FailFinalOnceSink",
    "FakeModelProvider",
    "LocalJsonlEventSink",
    "ModelInvocationService",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelRouterConfig",
    "Path",
    "RunCreate",
    "RunOrchestrator",
    "SQLAlchemyStorage",
    "SessionCreate",
    "UsageEvidenceContext",
    "_assert_settled_once",
    "_context",
    "_resolve_trace",
    "_seed_run",
    "asyncio",
    "pytest",
    "run_migrations",
    "stable_usage_call_id",
]
