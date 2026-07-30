"""普通文本流的 SDK usage、deadline、取消与 partial usage 合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.model_streaming_sdk_event_test_helpers import (
    AgentRunResultEvent,
    PartStartEvent,
    TextPart,
    patch_pydantic_stream_event_types,
)
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run
from tests.contracts.test_controlled_model_streaming_routing_contracts import (
    _policy,
    _request,
    _stream_settings,
)

from agent_harness.adapters.models._pydantic_ai_streaming import PreparedPydanticStream
from agent_harness.adapters.models.pydantic_ai import (
    PydanticAIModelProvider,
)
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    ModelStreamCloseResult,
    ModelStreamUsage,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import stream_group_id


@dataclass
class _BlockingStreamProvider:
    """首个 provider iteration 内等待取消，并返回测试指定的关闭证明。"""

    close_result: ModelStreamCloseResult
    provider_id: str = "fake"
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    close_calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream cancellation must not call complete")

    async def prepare_stream(self, request: ModelRequest, *, plan: ModelRoutePlan):  # type: ignore[no-untyped-def]
        provider = self

        class Prepared:
            def __aiter__(self):  # type: ignore[no-untyped-def]
                async def generate():  # type: ignore[no-untyped-def]
                    provider.entered.set()
                    await asyncio.Event().wait()
                    if False:  # pragma: no cover - 保持 async generator 协议
                        yield None

                return generate()

            async def result(self) -> ModelResponse:
                raise AssertionError("cancelled stream has no final result")

            async def aclose(self) -> ModelStreamCloseResult:
                provider.close_calls += 1
                return provider.close_result

        assert request.capability == plan.capability == "text_stream"
        return Prepared()


@dataclass
class _InvalidUsageSDKAgent:
    """返回非法 bool token usage，复现 adapter result/close 的重复解析失败。"""

    def __init__(self) -> None:
        self.iterations = 0
        self.exits = 0

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        assert model_settings is not None

        async def events():  # type: ignore[no-untyped-def]
            self.iterations += 1

            class Result:
                output = prompt

                def usage(self) -> object:
                    return SimpleNamespace(input_tokens=True, output_tokens=1)

            yield PartStartEvent(index=0, part=TextPart(content=prompt))
            yield AgentRunResultEvent(result=cast(Any, Result()))

        try:
            yield events()
        finally:
            self.exits += 1


class _NeverStartedSDKAgent:
    """deadline 已耗尽时，真实 adapter 不应创建 SDK stream context。"""

    def __init__(self) -> None:
        self.context_calls = 0

    def run_stream_events(self, prompt: str, *, model_settings: object) -> object:
        del prompt, model_settings
        self.context_calls += 1
        raise AssertionError("expired deadline must not create an SDK stream context")


class _ExpireBeforeSDKIterationProvider(PydanticAIModelProvider):
    """在真实 prepare 返回后确定性耗尽 deadline，覆盖 started 后零调用窗口。"""

    def __init__(self, *, agent: _NeverStartedSDKAgent) -> None:
        super().__init__(agent_factory=lambda _plan: agent)
        self.prepared_calls = 0

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedPydanticStream:
        prepared = await super().prepare_stream(request, plan=plan)
        self.prepared_calls += 1
        # 只控制测试所需的 monotonic deadline；调用仍从可信 bound façade
        # 进入真实 Pydantic adapter，且不得触达 SDK context/provider。
        cast(Any, prepared).deadline = asyncio.get_running_loop().time() - 1
        return prepared


@pytest.mark.asyncio
async def test_invalid_sdk_usage_enters_durable_needs_review_without_close_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 SDK usage 必须规范化为 unknown，并在 bound seam 原子保留全部围栏。"""

    patch_pydantic_stream_event_types(monkeypatch)
    settings = _stream_settings()
    agent = _InvalidUsageSDKAgent()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-invalid-sdk-usage.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-invalid-sdk-usage.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_provider="openai-compatible",
                default_model="fixture-text-1",
            ),
            providers={"openai-compatible": provider},
            model_settings=settings.model,
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                _request().model_copy(update={"prompt": "invalid usage"}),
                operation_key="invalid-sdk-usage",
            )

        events = await sink.read(run_id=run_id)
        started = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state
            usage_result = usage.result_json

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        assert group_states == ["started"] * 65
        assert usage_state == "needs_review"
        assert usage_result is not None and "attempt_review" in usage_result
        assert capacity.outstanding_reserved_event_count == 66
        assert agent.iterations == agent.exits == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_pydantic_deadline_before_sdk_iteration_settles_durable_not_started(
    tmp_path: Path,
) -> None:
    """durable started 后 deadline 到期仍须以零 provider 调用确定性收口。"""

    settings = _stream_settings()
    agent = _NeverStartedSDKAgent()
    provider = _ExpireBeforeSDKIterationProvider(agent=agent)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-pydantic-not-started.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-pydantic-not-started.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_provider="openai-compatible",
                default_model="fixture-text-1",
            ),
            providers={"openai-compatible": provider},
            model_settings=settings.model,
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                _request().model_copy(update={"prompt": "expire before SDK context"}),
                operation_key="pydantic-deadline-before-sdk-context",
            )

        events = await sink.read(run_id=run_id)
        started = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
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
        assert provider.prepared_calls == 1
        assert agent.context_calls == 0
        assert group_states == ["cancelled"] * 65
        assert usage_state == "published"
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_state", ["unknown", "stopped"])
async def test_cancelled_stream_respects_provider_stop_proof(
    tmp_path: Path,
    close_state: str,
) -> None:
    """unknown 保留全部围栏；stopped+complete 才取消占位并发布 cancelled usage。"""

    close_result = (
        ModelStreamCloseResult(state="unknown")
        if close_state == "unknown"
        else ModelStreamCloseResult(
            state="stopped",
            usage=ModelStreamUsage(
                finality="complete",
                input_tokens=0,
                output_tokens=0,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=0,
            ),
        )
    )
    provider = _BlockingStreamProvider(close_result=close_result)
    dsn = f"sqlite+aiosqlite:///{tmp_path / f'stream-{close_state}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / f"stream-{close_state}.jsonl", run_trace_resolver=resolve_trace
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
                    capability="text_stream",
                    prompt="cancel me",
                    max_output_tokens=8,
                ),
                operation_key="cancelled-stream",
            )
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await task

        events = await sink.read(run_id=run_id)
        usage_call_id = cast(dict[str, object], events[0].payload)["correlation"]
        usage_call_id = cast(dict[str, str], usage_call_id)["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            states = [item.state for item in group]
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            usage_state = usage.state
            capacity = await uow.event_capacity.snapshot(run_id)

        assert provider.close_calls == 1
        assert CanonicalEventType.MODEL_OUTPUT_COMPLETED not in {
            event.event_type for event in events
        }
        if close_state == "unknown":
            assert exc_info.value.code == "model.provider_side_effect_unknown"
            assert [event.event_type for event in events] == [
                CanonicalEventType.MODEL_REQUEST_STARTED
            ]
            assert states == ["started"] * 65
            assert usage_state == "needs_review"
            assert capacity.outstanding_reserved_event_count == 66
        else:
            assert exc_info.value.code == "model.invocation_cancelled"
            assert [event.event_type for event in events] == [
                CanonicalEventType.MODEL_REQUEST_STARTED,
                CanonicalEventType.MODEL_USAGE_UPDATED,
            ]
            assert states == ["cancelled"] * 65
            assert usage_state == "published"
            assert capacity.outstanding_reserved_event_count == 0
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_state", ["unknown", "stopped"])
@pytest.mark.parametrize("output_tokens", [None, 5])
async def test_partial_stream_usage_is_audited_without_releasing_any_fence(
    tmp_path: Path,
    close_state: str,
    output_tokens: int | None,
) -> None:
    """partial usage 即使 token 齐全也只形成 needs-review，不发布 final 或释放容量。"""

    provider = _BlockingStreamProvider(
        close_result=ModelStreamCloseResult(
            state=cast(Any, close_state),
            usage=ModelStreamUsage(
                finality="partial",
                input_tokens=3,
                output_tokens=output_tokens,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=7,
            ),
        )
    )
    dsn = f"sqlite+aiosqlite:///{tmp_path / f'stream-partial-{close_state}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / f"stream-partial-{close_state}.jsonl",
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
        task = asyncio.create_task(
            bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="audit partial usage",
                    max_output_tokens=8,
                ),
                operation_key=f"partial-{close_state}",
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
            usage_error_code = usage.error_code
            usage_result = usage.result_json
            outstanding = capacity.outstanding_reserved_event_count

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        assert group_states == ["started"] * 65
        assert usage_state == "needs_review"
        assert usage_error_code == "model.provider_side_effect_unknown"
        assert usage_result is not None
        review = cast(dict[str, Any], usage_result["attempt_review"])
        assert review["provider_close_state"] == close_state
        assert review["usage_finality"] == "partial"
        assert review["attempts"][0]["input_tokens"] == 3
        assert review["attempts"][0]["output_tokens"] == output_tokens
        assert review["budget_charge"] == {
            "charged_tokens": None,
            "charged_cost_usd": None,
            "charge_status": "unknown",
            "unresolved_attempts": [1],
        }
        assert outstanding == 66
    finally:
        await service.aclose()
        await storage.dispose()
