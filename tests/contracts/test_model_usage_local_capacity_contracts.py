"""SQLite/local usage event capacity 的临界序号合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import (
    event_bus,
    resolve_trace,
    seed_local_high_water,
    seed_run,
)

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import (
    CanonicalEventType,
    LocalJsonlEventSink,
)
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import MAX_EVENT_SEQ, EventCapacityExceeded


class CountingModelProvider(FakeModelProvider):
    """证明容量门禁发生在 model provider 副作用之前。"""

    def __init__(self) -> None:
        """初始化调用计数，确保容量拒绝路径可断言 provider 尚未产生副作用。"""

        self.calls = 0

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """记录调用后复用 fake provider 的合法结果，隔离本场景与模型实现细节。"""

        self.calls += 1
        return await super().complete(request, plan=plan)


class CountingEmbeddingProvider:
    """证明容量门禁发生在 embedding cache/provider 副作用之前。"""

    provider = "counting-embedding"
    model = "embedding-model"

    def __init__(self) -> None:
        """初始化 embedding 调用计数，供容量门禁前置断言使用。"""

        self.calls = 0

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """记录调用并返回最小有效 embedding，证明剩余容量足够时可正常执行。"""

        self.calls += 1
        return EmbeddingResponse(
            provider=self.provider,
            model=self.model,
            vector_ref="embedding://local-capacity/result",
            vector=[0.25],
            cache=EmbeddingCacheInfo(
                hit=False,
                input_hash="local-capacity-hash",
                vector_ref="embedding://local-capacity/result",
            ),
            latency_ms=1,
        )


def _context(run_id: str) -> UsageEvidenceContext:
    """构造与 seeded run 匹配的 usage 上下文，避免测试重复手写关联字段。"""

    return UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


async def _assert_last_two_slots_settled(
    *, storage: SQLAlchemyStorage, event_path: Path, run_id: str
) -> None:
    """断言最后两个可用序号被 request/final usage 完整消费并释放预约。"""

    events = await LocalJsonlEventSink(
        event_path,
        run_trace_resolver=resolve_trace,
    ).read(run_id=run_id)
    assert [event.event_type for event in events[-2:]] == [
        CanonicalEventType.MODEL_REQUEST_STARTED,
        CanonicalEventType.MODEL_USAGE_UPDATED,
    ]
    assert [event.seq for event in events] == [
        MAX_EVENT_SEQ - 3,
        MAX_EVENT_SEQ - 2,
        MAX_EVENT_SEQ - 1,
    ]
    async with storage.uow() as uow:
        capacity = await uow.event_capacity.snapshot(run_id)
    assert capacity.highest_persisted_seq == MAX_EVENT_SEQ - 1
    assert capacity.outstanding_reserved_event_count == 0
    assert capacity.terminal_reservation == 1


@pytest.mark.asyncio
async def test_local_model_invocation_consumes_each_of_last_two_reserved_slots(
    tmp_path: Path,
) -> None:
    """验证模型调用可精确占用本地容量的最后两个预留序号并正常结算。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-last-slots.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "model-last-slots.jsonl"
    provider = CountingModelProvider()
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 3,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=event_bus(storage=storage, event_path=event_path),
        )

        await service.complete(
            ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
            context=_context(run_id),
            usage_call_id="model-last-slots",
        )

        assert provider.calls == 1
        await _assert_last_two_slots_settled(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_local_embedding_invocation_consumes_each_of_last_two_reserved_slots(
    tmp_path: Path,
) -> None:
    """验证 embedding 调用与模型调用遵循相同的本地容量双事件预约语义。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-last-slots.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "embedding-last-slots.jsonl"
    provider = CountingEmbeddingProvider()
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 3,
        )
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=event_bus(storage=storage, event_path=event_path),
        )

        await service.embed(
            EmbeddingRequest(input="hello", tenant_id="tenant-a"),
            context=_context(run_id),
            usage_call_id="embedding-last-slots",
        )

        assert provider.calls == 1
        await _assert_last_two_slots_settled(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_kind", ["model", "embedding"])
async def test_local_usage_capacity_exhaustion_precedes_provider_side_effect(
    tmp_path: Path,
    usage_kind: str,
) -> None:
    """验证容量耗尽在 model/embedding provider 及 outbox 写入前关闭式失败。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / f'{usage_kind}-exhausted.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / f"{usage_kind}-exhausted.jsonl"
    model_provider = CountingModelProvider()
    embedding_provider = CountingEmbeddingProvider()
    try:
        run_id = await seed_run(storage)
        await seed_local_high_water(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            highest_seq=MAX_EVENT_SEQ - 2,
        )
        bus = event_bus(storage=storage, event_path=event_path)

        with pytest.raises(EventCapacityExceeded):
            if usage_kind == "model":
                await ModelInvocationService(
                    router=ModelRouter(
                        config=ModelRouterConfig(default_model="fake-basic"),
                        providers={"fake": model_provider},
                    ),
                    storage=storage,
                    event_bus=bus,
                ).complete(
                    ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                    context=_context(run_id),
                    usage_call_id="model-exhausted",
                )
            else:
                await EmbeddingInvocationService(
                    provider=embedding_provider,
                    storage=storage,
                    event_bus=bus,
                ).embed(
                    EmbeddingRequest(input="hello", tenant_id="tenant-a"),
                    context=_context(run_id),
                    usage_call_id="embedding-exhausted",
                )

        assert model_provider.calls == 0
        assert embedding_provider.calls == 0
        async with storage.uow() as uow:
            assert await uow.evidence_outbox.list_for_run(run_id=run_id) == []
    finally:
        await storage.dispose()
