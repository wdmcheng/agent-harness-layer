"""Model/embedding usage 的 provider、model 与 tenant 身份边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingProviderInvocationError,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelDecision,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


def _context(run_id: str, *, tenant_id: str = "tenant-a") -> UsageEvidenceContext:
    """构造与测试 run 对齐的使用证据上下文，并允许显式覆盖租户边界。"""

    return UsageEvidenceContext(
        tenant_id=tenant_id,
        run_id=run_id,
        agent_id="agent-a",
        trace_id="trace-a",
    )


@pytest.mark.asyncio
async def test_model_response_identity_must_match_routing_plan(tmp_path: Path) -> None:
    """验证模型 provider 返回的身份必须匹配已选路由，失配只产生脱敏失败证据。"""

    class MismatchedProvider:
        """故意返回与声明 route 不同 provider/model 的模型替身。"""

        provider_id = "declared"

        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            """构造身份失配但含私有输出的响应，覆盖服务端最后一道校验。"""

            return ModelResponse(
                provider="wrong-provider",
                model="wrong-model",
                output_text="private output",
                decision=ModelDecision(action="call", estimated_tokens=1),
                token_usage={"input_tokens": 1, "output_tokens": 1},
            )

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-identity.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "model-identity.jsonl", run_trace_resolver=resolve_trace)
    try:
        run_id = await seed_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="planned-model"),
                providers={"declared": MismatchedProvider()},
            ),
            storage=storage,
            event_bus=EventBus(
                sink=sink,
                run_trace_resolver=resolve_trace,
                capacity_storage=storage,
            ),
        )
        with pytest.raises(ModelProviderInvocationError):
            await service.complete(
                ModelRequest(provider="declared", prompt="private", max_output_tokens=1),
                context=_context(run_id),
                usage_call_id="model-identity",
            )

        events = await sink.read(run_id=run_id)
        assert events[-1].payload is not None
        final_usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert final_usage["provider"] == "declared"
        assert final_usage["model"] == "planned-model"
        assert events[-1].payload["outcome"] == "failed"
        assert "wrong-provider" not in (tmp_path / "model-identity.jsonl").read_text()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_request_tenant_must_match_authenticated_context(tmp_path: Path) -> None:
    """验证 embedding 请求租户必须等于认证上下文，失败前不得触发 cache 或 provider。"""

    class RecordingProvider:
        """记录是否被误调用的 provider 探针，正常路径在本场景不应到达。"""

        provider = "embedding-provider"
        model = "embedding-model"
        calls = 0

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            """一旦调用即明确失败，确保测试能发现租户检查被错误后置。"""

            self.calls += 1
            raise AssertionError("tenant mismatch must fail before provider/cache")

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-tenant.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "embedding-tenant.jsonl"
    provider = RecordingProvider()
    try:
        run_id = await seed_run(storage)
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(
                sink=LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace),
                run_trace_resolver=resolve_trace,
                capacity_storage=storage,
            ),
        )
        with pytest.raises(ValueError, match="tenant"):
            await service.embed(
                EmbeddingRequest(input="private", tenant_id="tenant-b"),
                context=_context(run_id),
                usage_call_id="embedding-tenant",
            )
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
        assert provider.calls == 0
        assert capacity.highest_persisted_seq == 0
        assert capacity.outstanding_reserved_event_count == 0
        assert not event_path.exists()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_response_identity_must_match_selected_provider(tmp_path: Path) -> None:
    """验证 embedding provider 不能在响应阶段篡改已选择的 provider/model 身份。"""

    class MismatchedProvider:
        """在执行时改写自身公开身份的恶意 provider 替身。"""

        provider = "declared-embedding"
        model = "planned-embedding"

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            """返回与初始声明不一致的响应，验证服务仅持久化冻结的路由身份。"""

            self.provider = "wrong-provider"
            self.model = "wrong-model"
            return EmbeddingResponse(
                provider=self.provider,
                model=self.model,
                vector_ref="embedding://private",
                vector=[1.0],
                cache=EmbeddingCacheInfo(
                    hit=False,
                    input_hash="private-hash",
                    vector_ref="embedding://private",
                ),
            )

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-identity.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "embedding-identity.jsonl",
        run_trace_resolver=resolve_trace,
    )
    try:
        run_id = await seed_run(storage)
        service = EmbeddingInvocationService(
            provider=MismatchedProvider(),
            storage=storage,
            event_bus=EventBus(
                sink=sink,
                run_trace_resolver=resolve_trace,
                capacity_storage=storage,
            ),
        )
        with pytest.raises(EmbeddingProviderInvocationError):
            await service.embed(
                EmbeddingRequest(input="private", tenant_id="tenant-a"),
                context=_context(run_id),
                usage_call_id="embedding-identity",
            )
        events = await sink.read(run_id=run_id)
        assert events[-1].payload is not None
        final_usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert final_usage["provider"] == "declared-embedding"
        assert final_usage["model"] == "planned-embedding"
        assert events[-1].payload["outcome"] == "failed"
    finally:
        await storage.dispose()
