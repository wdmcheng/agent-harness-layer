"""普通文本流的结果冲突、durable publish/ack 丢失与前置取消合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelDecision,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    ModelStreamCloseResult,
    ModelStreamDelta,
    ModelStreamUsage,
    PreparedModelStreamCall,
)
from agent_harness.models import _streaming_consumption as streaming_consumption
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import stream_group_id


@dataclass
class _BlockingPrepareProvider:
    """prepare 保持惰性且可取消，用于覆盖 started 后首次迭代前窗口。"""

    provider_id: str = "fake"
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream preparation must not call complete")

    async def prepare_stream(self, request: ModelRequest, *, plan: ModelRoutePlan):  # type: ignore[no-untyped-def]
        assert request.capability == plan.capability == "text_stream"
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled prepare must not return a stream")


@dataclass
class _MismatchedFinalProvider:
    """复现 provider-neutral delta 与最终文本冲突，同时声称已停止且计量完整。"""

    provider_id: str = "fake"
    close_calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream mismatch must not call complete")

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelStreamCall:
        provider = self

        class Prepared:
            def __aiter__(self) -> AsyncIterator[ModelStreamDelta]:
                async def generate() -> AsyncIterator[ModelStreamDelta]:
                    # 超过默认 1024-byte chunk target，确保冲突前已有一个 durable delta。
                    yield ModelStreamDelta(text="a" * 1032)

                return generate()

            async def result(self) -> ModelResponse:
                return ModelResponse(
                    provider="fake",
                    model=plan.model,
                    output_text="goodbye",
                    decision=ModelDecision(
                        action="call",
                        estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
                    ),
                    token_usage={"input_tokens": 1, "output_tokens": 1},
                    latency_ms=1,
                )

            async def aclose(self) -> ModelStreamCloseResult:
                provider.close_calls += 1
                return ModelStreamCloseResult(
                    state="stopped",
                    usage=ModelStreamUsage(
                        finality="complete",
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=None,
                        cost_status="unavailable",
                        latency_ms=1,
                    ),
                )

        assert request.capability == plan.capability == "text_stream"
        return Prepared()


@dataclass
class _StoppedCompleteDeltaProvider:
    """首个 delta 后可证明停止且计量完整，用于发布失败的耐久边界。"""

    provider_id: str = "fake"
    pulls: int = 0
    close_calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream publication failure must not call complete")

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelStreamCall:
        provider = self

        class Prepared:
            def __aiter__(self) -> AsyncIterator[ModelStreamDelta]:
                async def generate() -> AsyncIterator[ModelStreamDelta]:
                    provider.pulls += 1
                    # guard 会保留最长触发词后缀；2048 bytes 仍确保形成一个 1024-byte
                    # durable chunk 后才进入注入的公开发布失败。
                    yield ModelStreamDelta(text="x" * 2048)

                return generate()

            async def result(self) -> ModelResponse:
                raise AssertionError("failed delta publication has no final result")

            async def aclose(self) -> ModelStreamCloseResult:
                provider.close_calls += 1
                return ModelStreamCloseResult(
                    state="stopped",
                    usage=ModelStreamUsage(
                        finality="complete",
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=None,
                        cost_status="unavailable",
                        latency_ms=1,
                    ),
                )

        assert request.capability == plan.capability == "text_stream"
        return Prepared()


class _FailFirstDeltaSink(LocalJsonlEventSink):
    """在 delta intent 已耐久后拒绝首次公开写入。"""

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        if getattr(event, "event_type", None) is CanonicalEventType.MODEL_OUTPUT_DELTA:
            raise RuntimeError("injected delta publication failure")
        return await super().write(event, after_claim=after_claim)


@pytest.mark.asyncio
async def test_mismatched_final_text_forces_unknown_and_preserves_every_fence(
    tmp_path: Path,
) -> None:
    """最终文本冲突不能被 provider 的 stopped+complete 关闭证明降格为普通失败。"""

    provider = _MismatchedFinalProvider()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-final-mismatch.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-final-mismatch.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
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
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="mismatch",
                    estimated_input_tokens=1,
                    max_output_tokens=8,
                ),
                operation_key="final-mismatch",
            )

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state
            usage_error_code = usage.error_code
            outstanding = capacity.outstanding_reserved_event_count

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert provider.close_calls == 1
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_OUTPUT_DELTA,
        ]
        assert group_states == ["published", *(["started"] * 64)]
        assert usage_state == "needs_review"
        assert usage_error_code == "model.provider_side_effect_unknown"
        assert outstanding == 65
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_durable_delta_publication_failure_enters_needs_review_without_cancelling_slot(
    tmp_path: Path,
) -> None:
    """delta intent 已提交但公开失败时不得按偏小本地计数取消其槽位。"""

    provider = _StoppedCompleteDeltaProvider()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-delta-publication-failure.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = _FailFirstDeltaSink(
        tmp_path / "stream-delta-publication-failure.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
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
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="persist before publish",
                    max_output_tokens=8,
                ),
                operation_key="delta-publication-failure",
            )

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state
            usage_error_code = usage.error_code

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert exc_info.value.failure_domain == "runtime"
        assert provider.pulls == 1
        assert provider.close_calls == 1
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        assert group_states == ["result_persisted", *(["started"] * 64)]
        assert usage_state == "needs_review"
        assert usage_error_code == "model.provider_side_effect_unknown"
        assert capacity.outstanding_reserved_event_count == 66
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_durable_delta_commit_ack_loss_scans_beyond_zero_local_chunk_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """intent 已提交但 persist 尚未返回时，结算不能用本地零计数漏掉 durable delta。"""

    provider = _StoppedCompleteDeltaProvider()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-delta-commit-ack-loss.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-delta-commit-ack-loss.jsonl", run_trace_resolver=resolve_trace
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    original_persist_delta = streaming_consumption.persist_delta

    async def persist_then_lose_ack(*args: Any, **kwargs: Any) -> CanonicalEvent:
        await original_persist_delta(*args, **kwargs)
        raise RuntimeError("simulated commit acknowledgement loss")

    monkeypatch.setattr(streaming_consumption, "persist_delta", persist_then_lose_ack)
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
                    prompt="persist commit acknowledgement loss",
                    max_output_tokens=8,
                ),
                operation_key="delta-commit-ack-loss",
            )

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state
            usage_error_code = usage.error_code

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert exc_info.value.failure_domain == "runtime"
        assert provider.pulls == 1
        assert provider.close_calls == 1
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        assert group_states == ["result_persisted", *(["started"] * 64)]
        assert usage_state == "needs_review"
        assert usage_error_code == "model.provider_side_effect_unknown"
        assert capacity.outstanding_reserved_event_count == 66
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cancel_after_started_before_provider_iteration_settles_not_started(
    tmp_path: Path,
) -> None:
    """prepare 内取消仍保留 started，并取消 65 占位、发布零调用 cancelled usage。"""

    provider = _BlockingPrepareProvider()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-not-started.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-not-started.jsonl", run_trace_resolver=resolve_trace
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
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
        task = asyncio.create_task(
            bound.stream(
                ModelRequest(
                    capability="text_stream", prompt="cancel prepare", max_output_tokens=8
                ),
                operation_key="cancel-before-iteration",
            )
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await task

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state

        assert exc_info.value.code == "model.invocation_cancelled"
        assert exc_info.value.provider_called is False
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert group_states == ["cancelled"] * 65
        assert usage_state == "published"
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        await service.aclose()
        await storage.dispose()
