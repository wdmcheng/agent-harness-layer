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


def test_stable_usage_call_id_binds_run_and_semantic_operation_slot() -> None:
    context = _context("run-a")
    first = stable_usage_call_id(context=context, operation_key="agent:model-primary")
    replay = stable_usage_call_id(context=context, operation_key="agent:model-primary")
    another_slot = stable_usage_call_id(context=context, operation_key="agent:model-secondary")
    another_run = stable_usage_call_id(
        context=_context("run-b"),
        operation_key="agent:model-primary",
    )

    assert first == replay
    assert len(first) == 64
    assert first not in {another_slot, another_run}


@pytest.mark.asyncio
async def test_model_stable_call_id_sequential_retry_does_not_replay_provider(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-sequential.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "model-sequential.jsonl")
    provider = CountingModelProvider()
    try:
        run_id = await _seed_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=_resolve_trace),
        )
        request = ModelRequest(provider="fake", prompt="hello", max_output_tokens=1)

        await service.complete(
            request,
            context=_context(run_id),
            usage_call_id="stable-model-sequential",
        )
        with pytest.raises(RuntimeError, match="durable settlement"):
            await service.complete(
                request,
                context=_context(run_id),
                usage_call_id="stable-model-sequential",
            )

        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_stable_call_id_concurrent_retry_does_not_replay_provider(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-concurrent.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "model-concurrent.jsonl")
    provider = CountingModelProvider()
    try:
        run_id = await _seed_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=_resolve_trace),
        )
        request = ModelRequest(provider="fake", prompt="hello", max_output_tokens=1)

        results = await asyncio.gather(
            service.complete(
                request,
                context=_context(run_id),
                usage_call_id="stable-model-concurrent",
            ),
            service.complete(
                request,
                context=_context(run_id),
                usage_call_id="stable-model-concurrent",
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, ModelResponse) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert "durable settlement" in str(failures[0])
        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_stable_call_id_sequential_retry_does_not_replay_cache_or_provider(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-sequential.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-sequential.jsonl")
    provider = CountingEmbeddingProvider()
    try:
        run_id = await _seed_run(storage)
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=_resolve_trace),
        )
        request = EmbeddingRequest(input="private embedding", tenant_id="tenant-a")

        await service.embed(
            request,
            context=_context(run_id),
            usage_call_id="stable-embedding-sequential",
        )
        with pytest.raises(RuntimeError, match="durable settlement"):
            await service.embed(
                request,
                context=_context(run_id),
                usage_call_id="stable-embedding-sequential",
            )

        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_stable_call_id_concurrent_retry_does_not_replay_cache_or_provider(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-concurrent.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-concurrent.jsonl")
    release = asyncio.Event()
    provider = CountingEmbeddingProvider(release=release)
    try:
        run_id = await _seed_run(storage)
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=_resolve_trace),
        )
        request = EmbeddingRequest(input="private embedding", tenant_id="tenant-a")

        winner = asyncio.create_task(
            service.embed(
                request,
                context=_context(run_id),
                usage_call_id="stable-embedding-concurrent",
            )
        )
        await provider.started.wait()
        with pytest.raises(RuntimeError, match="durable settlement"):
            await service.embed(
                request,
                context=_context(run_id),
                usage_call_id="stable-embedding-concurrent",
            )
        release.set()
        await winner

        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=sink, run_id=run_id)
    finally:
        release.set()
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_retry_republishes_persisted_result_without_replaying_provider(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-result-persisted.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    durable_sink = LocalJsonlEventSink(
        tmp_path / "model-result-persisted.jsonl",
        run_trace_resolver=_resolve_trace,
    )
    provider = CountingModelProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    try:
        run_id = await _seed_run(storage)
        request = ModelRequest(provider="fake", prompt="hello", max_output_tokens=1)
        failing = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(
                sink=FailFinalOnceSink(durable_sink),
                run_trace_resolver=_resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="final write failure"):
            await failing.complete(
                request,
                context=_context(run_id),
                usage_call_id="stable-model-result-persisted",
            )

        recovering = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=_resolve_trace),
        )
        with pytest.raises(RuntimeError, match="durable settlement: published"):
            await recovering.complete(
                request,
                context=_context(run_id),
                usage_call_id="stable-model-result-persisted",
            )

        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=durable_sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_retry_republishes_persisted_result_without_replaying_provider(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-result-persisted.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    durable_sink = LocalJsonlEventSink(
        tmp_path / "embedding-result-persisted.jsonl",
        run_trace_resolver=_resolve_trace,
    )
    provider = CountingEmbeddingProvider()
    try:
        run_id = await _seed_run(storage)
        request = EmbeddingRequest(input="private embedding", tenant_id="tenant-a")
        failing = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(
                sink=FailFinalOnceSink(durable_sink),
                run_trace_resolver=_resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="final write failure"):
            await failing.embed(
                request,
                context=_context(run_id),
                usage_call_id="stable-embedding-result-persisted",
            )

        recovering = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=_resolve_trace),
        )
        with pytest.raises(RuntimeError, match="durable settlement: published"):
            await recovering.embed(
                request,
                context=_context(run_id),
                usage_call_id="stable-embedding-result-persisted",
            )

        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=durable_sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_runtime_startup_recovery_republishes_usage_before_executor_replay(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'runtime-recovery.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    durable_sink = LocalJsonlEventSink(
        tmp_path / "runtime-recovery.jsonl",
        run_trace_resolver=_resolve_trace,
    )
    provider = CountingModelProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    try:
        run_id = await _seed_run(storage)
        failing = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(
                sink=FailFinalOnceSink(durable_sink),
                run_trace_resolver=_resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="final write failure"):
            await failing.complete(
                ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                context=_context(run_id),
                usage_call_id="runtime-recovery",
            )

        recovering = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=_resolve_trace),
        )
        orchestrator = RunOrchestrator(
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=_resolve_trace),
            executor_services={"model_invocation": recovering},
        )
        assert await orchestrator.recover_pending_usage_evidence() == 1
        assert await orchestrator.recover_pending_usage_evidence() == 0
        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=durable_sink, run_id=run_id)

        worker_source = Path("templates/service-app/app/workers/runtime_worker.py").read_text(
            encoding="utf-8"
        )
        assert "await _recover_pending_usage(components)" in worker_source
    finally:
        await storage.dispose()
