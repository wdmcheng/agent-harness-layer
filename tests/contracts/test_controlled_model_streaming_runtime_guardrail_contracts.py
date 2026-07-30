"""可信 bound façade 到 durable CanonicalEvent 的 guardrail 运行时合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.config import ModelSettings
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelDecision,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    ModelStreamCloseResult,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


@dataclass
class _TwoChunkStreamProvider:
    """两段惰性流；用于证明慢持久化时不会预拉取第二段。"""

    provider_id: str = "fake"
    pulls: int = 0
    close_calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream deadline must not call complete")

    async def prepare_stream(self, request: ModelRequest, *, plan: ModelRoutePlan):  # type: ignore[no-untyped-def]
        provider = self

        class Prepared:
            def __aiter__(self):  # type: ignore[no-untyped-def]
                async def generate():  # type: ignore[no-untyped-def]
                    provider.pulls += 1
                    yield type("Delta", (), {"text": "first"})()
                    provider.pulls += 1
                    yield type("Delta", (), {"text": "second"})()

                return generate()

            async def result(self) -> ModelResponse:
                raise AssertionError("deadline stream has no final result")

            async def aclose(self) -> ModelStreamCloseResult:
                provider.close_calls += 1
                return ModelStreamCloseResult(state="unknown")

        assert request.capability == plan.capability == "text_stream"
        return Prepared()


class _SlowDeltaSink(LocalJsonlEventSink):
    """只阻塞公开 delta 写入，started 保持正常，模拟存储背压。"""

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        if event.event_type is CanonicalEventType.MODEL_OUTPUT_DELTA:
            await asyncio.sleep(2)
        return await super().write(event, after_claim=after_claim)


@dataclass
class _DelayedPrepareTailProvider:
    """prepare 消耗大部分总预算，唯一 fragment 只在 provider 结束后形成尾块。"""

    provider_id: str = "fake"
    pulls: int = 0
    close_calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("tail deadline must not call complete")

    async def prepare_stream(self, request: ModelRequest, *, plan: ModelRoutePlan):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.65)
        provider = self

        class Prepared:
            def __aiter__(self):  # type: ignore[no-untyped-def]
                async def generate():  # type: ignore[no-untyped-def]
                    provider.pulls += 1
                    yield type("Delta", (), {"text": "tail"})()

                return generate()

            async def result(self) -> ModelResponse:
                return ModelResponse(
                    provider="fake",
                    model=plan.model,
                    output_text="tail",
                    decision=ModelDecision(
                        action="call",
                        estimated_tokens=(
                            request.estimated_input_tokens + request.max_output_tokens
                        ),
                    ),
                    token_usage={"input_tokens": 1, "output_tokens": 1},
                    latency_ms=1,
                )

            async def aclose(self) -> ModelStreamCloseResult:
                provider.close_calls += 1
                return ModelStreamCloseResult(state="unknown")

        return Prepared()


class _DelayedTailDeltaSink(LocalJsonlEventSink):
    """只延迟 provider 自然结束后形成的尾部 delta 发布。"""

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        if event.event_type is CanonicalEventType.MODEL_OUTPUT_DELTA:
            await asyncio.sleep(0.6)
        return await super().write(event, after_claim=after_claim)


@pytest.mark.asyncio
async def test_prepare_and_tail_publish_share_one_absolute_route_deadline(
    tmp_path: Path,
) -> None:
    """prepare、provider 消费与尾块持久化不得各自重启完整 timeout。"""

    provider = _DelayedPrepareTailProvider()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-tail-deadline.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = _DelayedTailDeltaSink(
        tmp_path / "stream-tail-deadline.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_provider="fake",
                default_model="fake-basic",
                timeout_seconds=1,
            ),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        started_at = perf_counter()
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="one deadline",
                    max_output_tokens=8,
                ),
                operation_key="prepare-and-tail-deadline",
            )
        elapsed = perf_counter() - started_at

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert elapsed < 1.2
        assert provider.pulls == 1
        assert provider.close_calls == 1
        events = await sink.read(run_id=run_id)
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_storage_backpressure_uses_route_deadline_and_does_not_pull_next_delta(
    tmp_path: Path,
) -> None:
    """delta 发布超出总 deadline 时必须关闭流，且不能预拉取后续 provider 事件。"""

    provider = _TwoChunkStreamProvider()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-backpressure.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = _SlowDeltaSink(tmp_path / "stream-backpressure.jsonl", run_trace_resolver=resolve_trace)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_provider="fake",
                default_model="fake-basic",
                timeout_seconds=1,
            ),
            providers={"fake": provider},
            model_settings=ModelSettings(model_stream_chunk_utf8_bytes=1),
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="backpressure",
                    max_output_tokens=8,
                ),
                operation_key="slow-storage",
            )

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert provider.pulls == 1
        assert provider.close_calls == 1
        events = await sink.read(run_id=run_id)
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_full_result_guardrail_disables_speculative_delta(
    tmp_path: Path,
    allowed: bool,
) -> None:
    """可信完整结果 guardrail 判定前零公开输出；通过后才分片，拒绝则保持围栏。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / f'full-result-{allowed}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / f"full-result-{allowed}.jsonl", run_trace_resolver=resolve_trace
    )
    observed: list[str] = []

    def guard(text: str) -> bool:
        observed.append(text)
        return allowed

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
        stream_output_guardrail=guard,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        if allowed:
            response = await bound.stream(
                ModelRequest(capability="text_stream", prompt="guard me", max_output_tokens=8),
                operation_key="full-result-guard",
            )
            assert response.output_text == "fake:guard me"
        else:
            with pytest.raises(ModelProviderInvocationError) as exc_info:
                await bound.stream(
                    ModelRequest(capability="text_stream", prompt="guard me", max_output_tokens=8),
                    operation_key="full-result-guard",
                )
            assert exc_info.value.failure_domain == "runtime"
        events = await sink.read(run_id=run_id)
        assert observed == ["fake:guard me"]
        if allowed:
            assert CanonicalEventType.MODEL_OUTPUT_DELTA in {event.event_type for event in events}
        else:
            assert [event.event_type for event in events] == [
                CanonicalEventType.MODEL_REQUEST_STARTED,
                CanonicalEventType.MODEL_USAGE_UPDATED,
            ]
    finally:
        await service.aclose()
        await storage.dispose()
