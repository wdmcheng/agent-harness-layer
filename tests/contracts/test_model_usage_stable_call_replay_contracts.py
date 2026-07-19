"""模型与 embedding 稳定 call id 重放合同测试。"""

from __future__ import annotations

from tests.contracts.test_model_usage_idempotency_contracts import (
    CountingEmbeddingProvider as CountingEmbeddingProvider,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    CountingModelProvider as CountingModelProvider,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    EmbeddingInvocationService as EmbeddingInvocationService,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    EmbeddingRequest as EmbeddingRequest,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    FailFinalOnceSink as FailFinalOnceSink,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelInvocationService as ModelInvocationService,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelRequest as ModelRequest,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelResponse as ModelResponse,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelRouter as ModelRouter,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelRouterConfig as ModelRouterConfig,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    Path as Path,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _assert_settled_once as _assert_settled_once,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _context as _context,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _resolve_trace as _resolve_trace,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _seed_run as _seed_run,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    pytest as pytest,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    stable_usage_call_id as stable_usage_call_id,
)


def test_stable_usage_call_id_binds_run_and_semantic_operation_slot() -> None:
    """稳定调用 ID 必须同时绑定运行与语义操作位，既支持重试又不能跨运行或跨步骤复用。"""

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
    """同一模型调用串行重试应读取已结算结果，不能再次触发外部 provider 或新增证据。"""

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

        first = await service.complete(
            request,
            context=_context(run_id),
            usage_call_id="stable-model-sequential",
        )
        replay = await service.complete(
            request,
            context=_context(run_id),
            usage_call_id="stable-model-sequential",
        )

        assert provider.calls == 1
        assert replay == first
        await _assert_settled_once(storage=storage, sink=sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_stable_call_id_concurrent_retry_does_not_replay_provider(
    tmp_path: Path,
) -> None:
    """并发模型重试只能选出一个结算胜者，竞争请求明确失败而不放大 provider 副作用。"""

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
    """embedding 的串行重试复用持久化结算，不能重新命中缓存或调用向量 provider。"""

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

        first = await service.embed(
            request,
            context=_context(run_id),
            usage_call_id="stable-embedding-sequential",
        )
        replay = await service.embed(
            request,
            context=_context(run_id),
            usage_call_id="stable-embedding-sequential",
        )

        assert provider.calls == 1
        assert replay == first
        await _assert_settled_once(storage=storage, sink=sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_stable_call_id_concurrent_retry_does_not_replay_cache_or_provider(
    tmp_path: Path,
) -> None:
    """embedding 并发重试在首个调用未完成时必须围栏后来者，避免缓存和 provider 双重执行。"""

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
    """终态事件写入失败后，模型重试仅补发已持久化结果，不得重新调用 provider。"""

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
        replay = await recovering.complete(
            request,
            context=_context(run_id),
            usage_call_id="stable-model-result-persisted",
        )

        assert provider.calls == 1
        assert replay.output_text == "fake:hello"
        await _assert_settled_once(storage=storage, sink=durable_sink, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_retry_republishes_persisted_result_without_replaying_provider(
    tmp_path: Path,
) -> None:
    """embedding 的证据补发必须读取既有向量结果，避免重算导致成本和结果漂移。"""

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
        replay = await recovering.embed(
            request,
            context=_context(run_id),
            usage_call_id="stable-embedding-result-persisted",
        )

        assert provider.calls == 1
        assert replay.vector_ref == "embedding://counting/result"
        await _assert_settled_once(storage=storage, sink=durable_sink, run_id=run_id)
    finally:
        await storage.dispose()
