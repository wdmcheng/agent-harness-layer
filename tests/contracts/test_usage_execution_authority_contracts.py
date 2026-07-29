"""Usage invocation 的 runtime 权威身份与业务可见 seam 合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.runtime.executor import build_execution_context
from agent_harness.storage import SQLAlchemyStorage, run_migrations


class _CountingModelProvider(FakeModelProvider):
    """记录底层模型调用次数，以验证身份校验失败发生在外部副作用之前。"""

    def __init__(self) -> None:
        """从零开始计数，避免继承的 fake provider 状态干扰本合同的副作用断言。"""

        self.calls = 0

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """在委托基础 fake 前计数，使测试可区分“被拒绝”与“已调用后失败”。"""

        self.calls += 1
        return await super().complete(request, plan=plan)


class _CountingEmbeddingProvider:
    """最小 embedding provider 替身，暴露可验证的调用次数和稳定响应。"""

    provider = "embedding-provider"
    model = "embedding-model"

    def __init__(self) -> None:
        """初始化副作用计数器；其余 provider 元数据保持类级固定以减少噪声。"""

        self.calls = 0

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """返回可持久化的固定向量，用调用计数证明伪造身份未触达 provider。"""

        self.calls += 1
        return EmbeddingResponse(
            provider=self.provider,
            model=self.model,
            vector_ref="embedding://authority/vector",
            vector=[1.0],
            cache=EmbeddingCacheInfo(
                hit=False,
                input_hash="authority-hash",
                vector_ref="embedding://authority/vector",
            ),
        )


def _model_service(
    *, storage: SQLAlchemyStorage, event_path: Path, provider: _CountingModelProvider
) -> ModelInvocationService:
    """构造未经 runtime 绑定的模型服务，专门用于验证其拒绝伪造上下文的职责。"""

    return ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=event_path),
    )


def _embedding_service(
    *, storage: SQLAlchemyStorage, event_path: Path, provider: _CountingEmbeddingProvider
) -> EmbeddingInvocationService:
    """构造未经 runtime 绑定的 embedding 服务，与模型路径共享同一身份边界验证方式。"""

    return EmbeddingInvocationService(
        provider=provider,
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=event_path),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_kind", ["model", "embedding"])
async def test_raw_invocation_rejects_forged_run_correlation_before_side_effect(
    tmp_path: Path,
    usage_kind: str,
) -> None:
    """即使绕过业务 facade，repository 也必须以 AgentRun 拒绝伪造关联。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / f'{usage_kind}-authority.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / f"{usage_kind}-authority.jsonl"
    model_provider = _CountingModelProvider()
    embedding_provider = _CountingEmbeddingProvider()
    try:
        run_id = await seed_run(storage, request_id="request-authoritative")
        forged = UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-forged",
            request_id="request-forged",
            trace_id="trace-a",
        )
        with pytest.raises(ValueError, match="persisted run identity"):
            if usage_kind == "model":
                await _model_service(
                    storage=storage,
                    event_path=event_path,
                    provider=model_provider,
                ).complete(
                    ModelRequest(provider="fake", prompt="private", max_output_tokens=1),
                    context=forged,
                    usage_call_id="forged-model",
                )
            else:
                await _embedding_service(
                    storage=storage,
                    event_path=event_path,
                    provider=embedding_provider,
                ).embed(
                    EmbeddingRequest(input="private", tenant_id="tenant-a"),
                    context=forged,
                    usage_call_id="forged-embedding",
                )

        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
            outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert model_provider.calls == 0
        assert embedding_provider.calls == 0
        assert capacity.highest_persisted_seq == 0
        assert capacity.outstanding_reserved_event_count == 0
        assert outbox == []
        assert not event_path.exists()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_runtime_context_exposes_only_run_bound_usage_services(tmp_path: Path) -> None:
    """业务 executor 只提供 operation_key，不能提交 identity 或稳定调用 ID。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'bound-services.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "bound-services.jsonl"
    model_provider = _CountingModelProvider()
    embedding_provider = _CountingEmbeddingProvider()
    raw_model = _model_service(
        storage=storage,
        event_path=event_path,
        provider=model_provider,
    )
    raw_embedding = _embedding_service(
        storage=storage,
        event_path=event_path,
        provider=embedding_provider,
    )
    try:
        run_id = await seed_run(storage, request_id="request-authoritative")
        context = build_execution_context(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
                roles=[],
            ),
            services={
                "model_invocation": raw_model,
                "embedding_invocation": raw_embedding,
            },
            agent_id="agent-a",
            run_id=run_id,
            request_id="request-authoritative",
            trace_id="trace-a",
        )
        model = cast(Any, context.require_service("model_invocation"))
        embedding = cast(Any, context.require_service("embedding_invocation"))
        assert model is not raw_model
        assert embedding is not raw_embedding

        await model.complete(
            ModelRequest(provider="fake", prompt="private", max_output_tokens=1),
            operation_key="model-primary",
        )
        await embedding.embed(
            EmbeddingRequest(input="private", tenant_id="tenant-a"),
            operation_key="embedding-primary",
        )

        events = await LocalJsonlEventSink(event_path).read(run_id=run_id)
        usage_events = [
            item
            for item in events
            if item.event_type
            in {
                CanonicalEventType.MODEL_REQUEST_STARTED,
                CanonicalEventType.MODEL_USAGE_UPDATED,
            }
        ]
        assert len(usage_events) == 4
        assert all(item.agent_id == "agent-a" for item in usage_events)
        assert all(item.request_id == "request-authoritative" for item in usage_events)
        assert all(item.trace_id == "trace-a" for item in usage_events)
        assert model_provider.calls == 1
        assert embedding_provider.calls == 1
    finally:
        await storage.dispose()
