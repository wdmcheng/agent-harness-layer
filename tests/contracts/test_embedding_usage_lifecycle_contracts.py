"""Embedding usage cache、provider 归一化与失败封闭合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.observability import TelemetryFacade, TelemetryRecord, TelemetryStatus
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import EmbeddingCacheCreate


class RecordingUsageTelemetryProvider:
    """记录 embedding started 与 final fan-out 的测试 provider。"""

    provider_name = "recording-usage"

    def __init__(self) -> None:
        """初始化 telemetry 记录列表，供 started/final fan-out 顺序断言。"""

        self.records: list[TelemetryRecord] = []

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        """保存收到的脱敏 telemetry 记录并模拟 provider 成功发送。"""

        self.records.append(record)
        return TelemetryStatus(provider=self.provider_name, status="sent")


async def _usage_run(storage: SQLAlchemyStorage) -> str:
    """创建固定身份和 trace 的最小 run，供 embedding 与模型 usage 测试复用。"""

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
async def test_embedding_cache_hit_has_usage_lifecycle_without_provider_side_effect(
    tmp_path: Path,
) -> None:
    """验证 embedding 缓存命中仍发布完整 usage 生命周期，但不会触发真实 provider 调用。"""

    class CachedEmbeddingProvider:
        """返回稳定缓存命中响应并分别记录查找和外部调用次数的 embedding provider 替身。"""

        provider = "openai-compatible"
        model = "text-embedding-test"

        def __init__(self) -> None:
            """初始化缓存查找与 provider 调用计数。"""

            self.lookup_calls = 0
            self.provider_calls = 0

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            """确认测试输入后返回命中响应，不增加 provider 调用计数。"""

            self.lookup_calls += 1
            assert request.input == "cached input"
            return EmbeddingResponse(
                provider=self.provider,
                model=self.model,
                vector_ref="embedding://cached",
                vector=[],
                cache=EmbeddingCacheInfo(
                    hit=True,
                    input_hash="hash-a",
                    vector_ref="embedding://cached",
                ),
                latency_ms=7,
            )

    database = tmp_path / "embedding-usage.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        """为本地事件 sink 返回固定 trace，隔离测试对存储查询的依赖。"""

        return "trace-a"

    sink = LocalJsonlEventSink(
        tmp_path / "embedding-events.jsonl",
        run_trace_resolver=resolve_trace,
    )
    provider = CachedEmbeddingProvider()
    telemetry_provider = RecordingUsageTelemetryProvider()
    try:
        run_id = await _usage_run(storage)
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            telemetry=TelemetryFacade(local_sink=sink, providers=[telemetry_provider]),
        )
        response = await service.embed(
            EmbeddingRequest(input="cached input", tenant_id="tenant-a"),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                request_id="request-a",
                trace_id="trace-a",
            ),
            usage_call_id="embedding-hit",
        )

        events = await sink.read(run_id=run_id)
        assert events[-1].payload is not None
        usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert response.cache.hit is True
        assert provider.lookup_calls == 1
        assert provider.provider_calls == 0
        assert [item.event_type for item in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert usage["usage_kind"] == "embedding"
        assert usage["latency_ms"] == 7
        assert usage["input_tokens"] is None
        assert usage["output_tokens"] is None
        assert usage["cost_usd"] is None
        assert usage["cost_status"] == "unavailable"
        assert usage["decision"] == {
            "cache_status": "hit",
            "provider_called": False,
        }
        assert [record.name for record in telemetry_provider.records] == [
            "agent_harness.model.request.started",
            "agent_harness.model.usage.updated",
        ]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_openai_compatible_embedding_maps_only_provider_neutral_usage(
    tmp_path: Path,
) -> None:
    """验证 OpenAI 兼容响应只投影 provider-neutral usage，秘密、原始向量和输入不落事件。"""

    from agent_harness.adapters.models.openai_compatible_embeddings import (
        OpenAICompatibleEmbeddingProvider,
    )

    class MemoryCache:
        """记录 cache 写入但始终未命中的最小缓存替身。"""

        def __init__(self) -> None:
            """初始化写入记录，供断言 adapter 仅保存预期 cache 项。"""

            self.created: list[EmbeddingCacheCreate] = []

        async def get(self, **_: object) -> None:
            """始终返回未命中，使测试走真实 provider 请求和 cache 写入路径。"""

            return None

        async def put(self, item: EmbeddingCacheCreate) -> None:
            """记录 adapter 写入的 cache DTO，不执行真实存储。"""

            self.created.append(item)

    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """断言请求确实携带私有输入和认证头，再返回含敏感字段的模拟 provider 响应。"""

        nonlocal provider_calls
        provider_calls += 1
        assert request.headers["authorization"] == "Bearer adapter-secret"
        assert b"private embedding input" in request.content
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.25, 0.5]}],
                "raw_secret": "provider-raw-secret",
                "usage": {"prompt_tokens": 99},
            },
        )

    database = tmp_path / "openai-embedding-usage.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "openai-embedding-events.jsonl")
    cache = MemoryCache()

    async def resolve_trace(**_: object) -> str:
        """为 OpenAI 兼容 adapter 事件提供固定 trace。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleEmbeddingProvider(
                cache=cast(Any, cache),
                base_url="https://embedding.example/v1",
                model="text-embedding-test",
                api_key="adapter-secret",
                client=client,
            )
            service = EmbeddingInvocationService(
                provider=provider,
                storage=storage,
                event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            )
            response = await service.embed(
                EmbeddingRequest(input="private embedding input", tenant_id="tenant-a"),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    request_id="request-a",
                    trace_id="trace-a",
                ),
                usage_call_id="embedding-openai",
            )

        events = await sink.read(run_id=run_id)
        assert provider_calls == 1
        assert len(cache.created) == 1
        assert response.vector == [0.25, 0.5]
        assert events[-1].payload is not None
        usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert usage["usage_kind"] == "embedding"
        assert usage["provider"] == "openai-compatible"
        assert usage["model"] == "text-embedding-test"
        assert usage["input_tokens"] == len(b"private embedding input")
        assert usage["output_tokens"] is None
        assert usage["cost_usd"] is None
        assert usage["cost_status"] == "unavailable"
        assert usage["decision"] == {"cache_status": "miss", "provider_called": True}
        serialized = (tmp_path / "openai-embedding-events.jsonl").read_text(encoding="utf-8")
        assert "private embedding input" not in serialized
        assert "adapter-secret" not in serialized
        assert "provider-raw-secret" not in serialized
        assert "prompt_tokens" not in serialized
        assert "0.25" not in serialized
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_provider_exception_is_closed_in_event_and_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 embedding provider 原异常被封闭，输入、认证信息和原始向量不进入异常或证据。"""

    import agent_harness.embeddings.invocation as embedding_invocation
    from agent_harness.embeddings import EmbeddingProviderInvocationError

    class LeakingEmbeddingProvider:
        """故意在异常文本中拼接输入、认证与向量内容的 provider，用于验证脱敏边界。"""

        provider = "leaking-embedding"
        model = "embedding-model"

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            """抛出包含敏感文本的原始异常，供调用服务封闭并测试其输出。"""

            raise RuntimeError(
                f"input={request.input}; Authorization=Bearer embedding-secret; raw vector=[1,2]"
            )

    database = tmp_path / "embedding-failure.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-failure.jsonl")
    clock = iter((100.0, 100.042))
    monkeypatch.setattr(embedding_invocation, "perf_counter", lambda: next(clock), raising=False)

    async def resolve_trace(**_: object) -> str:
        """为失败场景的本地事件 sink 返回固定 trace。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = EmbeddingInvocationService(
            provider=LeakingEmbeddingProvider(),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        with pytest.raises(EmbeddingProviderInvocationError) as exc_info:
            await service.embed(
                EmbeddingRequest(input="private embedding failure", tenant_id="tenant-a"),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="embedding-failure",
            )

        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="embedding-failure",
            )
            serialized_outbox = json.dumps(outbox.result_json, ensure_ascii=False)
        serialized_event = (tmp_path / "embedding-failure.jsonl").read_text(encoding="utf-8")
        events = await sink.read(run_id=run_id)
        assert events[-1].payload is not None
        failed_usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert exc_info.value.code == "embedding.provider_failed"
        assert failed_usage["latency_ms"] == 42
        assert failed_usage["decision"]["provider_called"] is True
        for secret in ("private embedding failure", "embedding-secret", "raw vector"):
            assert secret not in str(exc_info.value)
            assert secret not in serialized_outbox
            assert secret not in serialized_event
        assert "embedding.provider_failed" in serialized_event
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_provider_failure_closes_raw_prompt_secret_and_response(tmp_path: Path) -> None:
    """验证模型 provider 异常同样封闭原 prompt、认证 secret 与响应片段。"""

    class LeakingProvider(FakeModelProvider):
        """在模型调用异常中故意嵌入 prompt、认证和响应内容的 fake provider。"""

        async def complete(self, request: ModelRequest, *, plan: object):
            """抛出泄露形态的异常，供模型调用服务验证稳定错误和事件脱敏。"""

            del plan
            raise RuntimeError(
                f"raw prompt={request.prompt}; Authorization=Bearer secret-token; "
                "response={'private': true}"
            )

    database = tmp_path / "provider-failure.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        """为模型失败事件提供固定 trace，避免测试混入 trace 解析路径。"""

        return "trace-a"

    sink = LocalJsonlEventSink(
        tmp_path / "provider-failure.jsonl",
        run_trace_resolver=resolve_trace,
    )
    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": LeakingProvider()},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await service.complete(
                ModelRequest(
                    provider="fake",
                    prompt="private prompt",
                    max_output_tokens=1,
                ),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-failed",
            )
        serialized = (tmp_path / "provider-failure.jsonl").read_text(encoding="utf-8")
        assert exc_info.value.code == "model.provider_failed"
        assert "private prompt" not in str(exc_info.value)
        assert "secret-token" not in str(exc_info.value)
        assert "private prompt" not in serialized
        assert "secret-token" not in serialized
        assert "private" not in serialized
        assert "model.provider_failed" in serialized
    finally:
        await storage.dispose()
