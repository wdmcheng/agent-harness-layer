"""Provider 已产生副作用后返回无效 usage 数据时的失败结算合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingProviderInvocationError,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
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


def _context(run_id: str) -> UsageEvidenceContext:
    return UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("token_usage", {"input_tokens": -1}),
        ("token_usage", {"input_tokens": True}),
        ("latency_ms", -1),
        ("latency_ms", True),
        ("cost_usd", float("inf")),
        ("cost_usd", -0.01),
    ],
)
def test_model_response_rejects_invalid_usage_metrics(
    field_name: str,
    field_value: object,
) -> None:
    payload: dict[str, object] = {
        "provider": "provider-a",
        "model": "model-a",
        "output_text": "private",
        "decision": ModelDecision(action="call", estimated_tokens=1),
        "token_usage": {"input_tokens": 1},
        "latency_ms": 1,
        "cost_usd": None,
        "cost_status": "unavailable",
    }
    payload[field_name] = field_value
    with pytest.raises(ValueError):
        ModelResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("latency_ms", "cache_hit"),
    [(-1, False), (True, False), ("1", False), (1, "false")],
)
def test_embedding_response_rejects_invalid_usage_metrics(
    latency_ms: object,
    cache_hit: object,
) -> None:
    with pytest.raises(ValueError):
        EmbeddingResponse.model_validate(
            {
                "provider": "provider-a",
                "model": "model-a",
                "vector_ref": "embedding://a",
                "vector": [1.0],
                "cache": {
                    "hit": cache_hit,
                    "input_hash": "hash-a",
                    "vector_ref": "embedding://a",
                },
                "latency_ms": latency_ms,
            }
        )


async def _assert_failed_settlement(
    *,
    storage: SQLAlchemyStorage,
    event_path: Path,
    run_id: str,
    error_code: str,
) -> None:
    events = await LocalJsonlEventSink(event_path).read(run_id=run_id)
    assert [item.event_type for item in events] == [
        CanonicalEventType.MODEL_REQUEST_STARTED,
        CanonicalEventType.MODEL_USAGE_UPDATED,
    ]
    assert events[-1].payload is not None
    assert events[-1].payload["outcome"] == "failed"
    assert events[-1].payload["error_code"] == error_code
    serialized = event_path.read_text(encoding="utf-8")
    assert "private-provider-payload" not in serialized
    async with storage.uow() as uow:
        capacity = await uow.event_capacity.snapshot(run_id)
        outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
        outbox_state = outbox[0].state if outbox else None
        outbox_error_code = outbox[0].error_code if outbox else None
    assert capacity.highest_persisted_seq == 2
    assert capacity.outstanding_reserved_event_count == 0
    assert len(outbox) == 1
    assert outbox_state == "published"
    assert outbox_error_code == error_code


@pytest.mark.asyncio
async def test_invalid_model_usage_return_closes_failed_settlement(tmp_path: Path) -> None:
    class InvalidUsageProvider:
        provider_id = "invalid-model"
        calls = 0

        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            self.calls += 1
            # model_construct 模拟恶意或损坏 adapter 绕过边界 DTO 校验。
            return ModelResponse.model_construct(
                provider=self.provider_id,
                model=model,
                output_text="private-provider-payload",
                decision=ModelDecision(action="call", estimated_tokens=1),
                token_usage={"input_tokens": -1, "output_tokens": 1},
                latency_ms=1,
                cost_usd=None,
                cost_status="unavailable",
            )

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'invalid-model.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "invalid-model.jsonl"
    provider = InvalidUsageProvider()
    try:
        run_id = await seed_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="model-a"),
                providers={provider.provider_id: provider},
            ),
            storage=storage,
            event_bus=event_bus(storage=storage, event_path=event_path),
        )
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await service.complete(
                ModelRequest(provider=provider.provider_id, prompt="private", max_output_tokens=1),
                context=_context(run_id),
                usage_call_id="invalid-model-usage",
            )
        assert exc_info.value.code == "model.provider_failed"
        assert str(exc_info.value) == "model provider invocation failed"
        assert provider.calls == 1
        await _assert_failed_settlement(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            error_code="model.provider_failed",
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_invalid_embedding_usage_return_closes_failed_settlement(tmp_path: Path) -> None:
    class InvalidUsageProvider:
        provider = "invalid-embedding"
        model = "embedding-a"
        calls = 0

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            self.calls += 1
            return EmbeddingResponse.model_construct(
                provider=self.provider,
                model=self.model,
                vector_ref="embedding://private-provider-payload",
                vector=[1.0],
                cache=EmbeddingCacheInfo(
                    hit=False,
                    input_hash="private-provider-payload",
                    vector_ref="embedding://private-provider-payload",
                ),
                latency_ms=-1,
            )

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'invalid-embedding.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "invalid-embedding.jsonl"
    provider = InvalidUsageProvider()
    try:
        run_id = await seed_run(storage)
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=event_bus(storage=storage, event_path=event_path),
        )
        with pytest.raises(EmbeddingProviderInvocationError) as exc_info:
            await service.embed(
                EmbeddingRequest(input="private", tenant_id="tenant-a"),
                context=_context(run_id),
                usage_call_id="invalid-embedding-usage",
            )
        assert exc_info.value.code == "embedding.provider_failed"
        assert str(exc_info.value) == "embedding provider invocation failed"
        assert provider.calls == 1
        await _assert_failed_settlement(
            storage=storage,
            event_path=event_path,
            run_id=run_id,
            error_code="embedding.provider_failed",
        )
    finally:
        await storage.dispose()
