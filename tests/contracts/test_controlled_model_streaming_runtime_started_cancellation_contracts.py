"""内部 started 已提交后、provider 首次迭代前的取消合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.config import load_settings
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.observability import (
    TelemetryFacade,
    TelemetryPublishResult,
    TelemetryStatus,
)
from agent_harness.registry import AgentModelPolicy
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import stream_group_id


class _NeverPreparedStreamProvider:
    """telemetry 等待期间取消时，provider prepare 与迭代都必须保持为零。"""

    provider_id = "fake"

    def __init__(self) -> None:
        self.prepare_calls = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream cancellation must not call complete")

    async def prepare_stream(self, request: ModelRequest, *, plan: ModelRoutePlan) -> object:
        del request, plan
        self.prepare_calls += 1
        raise AssertionError("started telemetry cancellation must precede prepare")


class _BlockingStartedTelemetry:
    """在 durable started 的 telemetry fan-out 点确定性暴露取消窗口。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def publish_event(self, event: CanonicalEvent) -> TelemetryPublishResult:
        if event.event_type == CanonicalEventType.MODEL_REQUEST_STARTED:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("blocking telemetry must be cancelled")
        assert event.event_type == CanonicalEventType.MODEL_USAGE_UPDATED
        return TelemetryPublishResult(
            local_status=TelemetryStatus(provider="test", status="already_written"),
            provider_statuses=[],
        )


class _BlockingPrepareStreamProvider:
    """prepare 已进入但保持无副作用，交由 runtime absolute deadline 取消。"""

    provider_id = "fake"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.prepare_calls = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        raise AssertionError("stream deadline must not call complete")

    async def prepare_stream(self, request: ModelRequest, *, plan: ModelRoutePlan) -> object:
        assert request.capability == plan.capability == "text_stream"
        self.prepare_calls += 1
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("runtime deadline must cancel blocking prepare")


class _DelayedStartedTelemetry:
    """让 started telemetry 明确耗时，同时保持 usage final 可立即收口。"""

    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    async def publish_event(self, event: CanonicalEvent) -> TelemetryPublishResult:
        if event.event_type == CanonicalEventType.MODEL_REQUEST_STARTED:
            await asyncio.sleep(self._delay_seconds)
        else:
            assert event.event_type == CanonicalEventType.MODEL_USAGE_UPDATED
        return TelemetryPublishResult(
            local_status=TelemetryStatus(provider="test", status="already_written"),
            provider_statuses=[],
        )


class _NeverEnteredPydanticAgent:
    """client lease 可创建，但自然 deadline 前绝不能进入 SDK stream context。"""

    def __init__(self) -> None:
        self.context_calls = 0

    def run_stream_events(self, prompt: str, *, model_settings: object) -> object:
        del prompt, model_settings
        self.context_calls += 1
        raise AssertionError("runtime deadline must expire before SDK stream context")


class _BlockingClientFactory:
    """首轮 client acquisition 阻塞；收口后第二轮用于证明 permit 已释放。"""

    def __init__(self, agent: _NeverEnteredPydanticAgent) -> None:
        self._agent = agent
        self.entered = asyncio.Event()
        self.acquire_calls = 0
        self.close_calls = 0

    async def acquire(self, plan: ModelRoutePlan) -> object:
        assert plan.capability == "text_stream"
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("runtime timeout must cancel blocked client acquisition")
        return SimpleNamespace(agent=self._agent)

    async def aclose(self) -> None:
        self.close_calls += 1


def _pydantic_deadline_settings() -> Any:
    """缩短受控 deployment deadline，保持真实 typed route 与 adapter composition。"""

    overrides = real_model_override()
    model = cast(dict[str, Any], overrides["model"])
    deployments = cast(dict[str, dict[str, Any]], model["deployments"])
    deployment = deployments["real_primary"]
    deployment["capabilities"] = ["text_completion", "text_stream"]
    deployment["connect_timeout_ms"] = 100
    deployment["read_timeout_ms"] = 100
    deployment["total_timeout_ms"] = 250
    return load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def _pydantic_policy() -> AgentModelPolicy:
    """把测试请求限制到唯一受控 deployment/model，不复用其他测试的私有夹具。"""

    return AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )


def _pydantic_request() -> ModelRequest:
    """构造真实 adapter public seam 所需的固定普通文本流请求。"""

    return ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        capability="text_stream",
        prompt="natural pydantic deadline",
        max_output_tokens=8,
    )


@pytest.mark.asyncio
async def test_cancel_during_started_telemetry_settles_durable_not_started(
    tmp_path: Path,
) -> None:
    """started 保持，高水位不倒退，零调用取消须闭合容量与 usage。"""

    provider = _NeverPreparedStreamProvider()
    telemetry = _BlockingStartedTelemetry()
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-started-telemetry-cancel.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-started-telemetry-cancel.jsonl",
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
        telemetry=cast(TelemetryFacade, telemetry),
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
        task = asyncio.create_task(
            bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="cancel during started telemetry",
                    max_output_tokens=8,
                ),
                operation_key="started-telemetry-cancel",
            )
        )
        await asyncio.wait_for(telemetry.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await task

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        final_payload = cast(dict[str, Any], events[1].payload)
        final_usage = cast(dict[str, Any], final_payload["usage"])
        final_decision = cast(dict[str, Any], final_usage["decision"])
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
        assert provider.prepare_calls == 0
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert final_payload["outcome"] == "cancelled"
        assert final_usage["input_tokens"] is None
        assert final_usage["output_tokens"] is None
        assert final_usage["cost_usd"] is None
        assert final_usage["cost_status"] == "unavailable"
        assert final_decision["provider_called"] is False
        assert group_states == ["cancelled"] * 65
        assert usage_state == "published"
        assert capacity.highest_persisted_seq == 2
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_runtime_deadline_after_started_telemetry_settles_not_started_as_cancelled(
    tmp_path: Path,
) -> None:
    """telemetry 不消耗 route 预算，prepare 前到期仍按 not-started 取消。"""

    provider = _BlockingPrepareStreamProvider()
    telemetry_delay_seconds = 1.1
    telemetry = _DelayedStartedTelemetry(telemetry_delay_seconds)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-started-telemetry-deadline.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-started-telemetry-deadline.jsonl",
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
        telemetry=cast(TelemetryFacade, telemetry),
    )
    try:
        run_id = await seed_run(storage, request_id="request-b")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-b",
            trace_id="trace-a",
        )
        started_at = perf_counter()
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="deadline after started telemetry",
                    max_output_tokens=8,
                ),
                operation_key="started-telemetry-deadline",
            )
        elapsed_seconds = perf_counter() - started_at

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        final_payload = cast(dict[str, Any], events[1].payload)
        final_usage = cast(dict[str, Any], final_payload["usage"])
        final_decision = cast(dict[str, Any], final_usage["decision"])
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
        assert provider.prepare_calls == 1
        assert provider.entered.is_set()
        assert elapsed_seconds >= telemetry_delay_seconds + 0.9
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert final_payload["outcome"] == "cancelled"
        assert final_usage["input_tokens"] is None
        assert final_usage["output_tokens"] is None
        assert final_usage["cost_usd"] is None
        assert final_usage["cost_status"] == "unavailable"
        assert final_decision["provider_called"] is False
        assert group_states == ["cancelled"] * 65
        assert usage_state == "published"
        assert capacity.highest_persisted_seq == 2
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_pydantic_client_acquire_naturally_times_out_after_started_telemetry(
    tmp_path: Path,
) -> None:
    """真实 adapter 的 client/permit seam 只由 runtime deadline 自然取消。"""

    settings = _pydantic_deadline_settings()
    agent = _NeverEnteredPydanticAgent()
    client_factory = _BlockingClientFactory(agent)
    provider = PydanticAIModelProvider(client_factory=cast(Any, client_factory))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    telemetry_delay_seconds = 0.2
    telemetry = _DelayedStartedTelemetry(telemetry_delay_seconds)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-pydantic-natural-deadline.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-pydantic-natural-deadline.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        telemetry=cast(TelemetryFacade, telemetry),
        agent_policy_resolver=lambda _agent_id: _pydantic_policy(),
    )
    try:
        run_id = await seed_run(storage, request_id="request-c")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-c",
            trace_id="trace-a",
        )
        started_at = perf_counter()
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await bound.stream(
                _pydantic_request(),
                operation_key="pydantic-natural-deadline",
            )
        elapsed_seconds = perf_counter() - started_at

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        final_payload = cast(dict[str, Any], events[1].payload)
        final_usage = cast(dict[str, Any], final_payload["usage"])
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
        assert client_factory.entered.is_set()
        assert client_factory.acquire_calls == 1
        assert agent.context_calls == 0
        assert elapsed_seconds >= telemetry_delay_seconds + 0.2
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert final_payload["outcome"] == "cancelled"
        assert final_usage["input_tokens"] is None
        assert final_usage["output_tokens"] is None
        assert final_usage["cost_usd"] is None
        assert final_usage["cost_status"] == "unavailable"
        assert group_states == ["cancelled"] * 65
        assert usage_state == "published"
        assert capacity.highest_persisted_seq == 2
        assert capacity.outstanding_reserved_event_count == 0

        # 使用同一 provider/deployment 再次取得并关闭 prepared stream，证明首轮
        # 取消已释放 process-local permit，且未把 client lease 泄漏到 SDK context。
        plan = router.plan(_pydantic_request(), agent_policy=_pydantic_policy())
        prepared = await asyncio.wait_for(
            provider.prepare_stream(_pydantic_request(), plan=plan),
            timeout=0.5,
        )
        await prepared.aclose()
        assert client_factory.acquire_calls == 2
        assert agent.context_calls == 0
    finally:
        await service.aclose()
        await storage.dispose()

    assert client_factory.close_calls == 1
