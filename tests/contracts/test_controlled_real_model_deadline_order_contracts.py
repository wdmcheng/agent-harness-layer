"""真实调用动态 route、deadline、执行顺序与 pre-send 围栏合同。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.controlled_real_model_retry_budget_test_support import (
    SequenceAgent,
    retry_route,
)
from tests.contracts.controlled_real_model_runtime_composition_test_support import ResultDouble
from tests.contracts.test_model_usage_invocation_contracts import usage_run

from agent_harness.adapters.models.pydantic_ai import (
    ModelProviderError,
    PydanticAIModelProvider,
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
)
from agent_harness.models.router import ModelRouteError
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage import SQLAlchemyStorage, run_migrations


def test_invalid_dynamic_route_rejects_before_reservation_client_and_network() -> None:
    """prompt/output 越界在执行 seam 前拒绝，因此 provider call count 保持零。"""

    model_settings, _request, policy = retry_route(classifier=False)
    agent = SequenceAgent([ResultDouble()])
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: agent,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    with pytest.raises(ModelRouteError):
        router.plan(
            ModelRequest(
                deployment_id="real_primary",
                provider="openai-compatible",
                prompt="x",
                max_output_tokens=999,
            ),
            agent_policy=policy,
        )
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_bulkhead_deadline_cancel_and_unknown_are_fenced() -> None:
    """total deadline 后完成状态未知，不得重试或静默 fallback fake。"""

    model_settings, request, policy = retry_route(classifier=True)

    class SlowAgent:
        calls = 0

        async def run(self, prompt: str, *, model_settings: object) -> ResultDouble:
            del prompt, model_settings
            self.calls += 1
            await asyncio.sleep(10)
            return ResultDouble()

    slow = SlowAgent()
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: slow,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy).model_copy(update={"total_timeout_ms": 5})
    with pytest.raises(ModelProviderError) as exc_info:
        await router.execute(request, plan=plan)
    assert exc_info.value.code == "model.provider_side_effect_unknown"
    assert slow.calls == 1


@pytest.mark.asyncio
async def test_total_deadline_includes_bulkhead_queue_and_client_acquire() -> None:
    """冻结 total deadline 必须从 prepare 开始覆盖排队与 lazy client lease。"""

    model_settings, request, policy = retry_route(classifier=False)
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: SequenceAgent([ResultDouble()]),
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    plan = plan.model_copy(
        update={
            "total_timeout_ms": 5,
            "bulkhead_policy": plan.bulkhead_policy.model_copy(
                update={"max_in_flight": 1, "queue_timeout_ms": 100}
            ),
        }
    )
    held = await provider.prepare(request, plan=plan.model_copy(update={"total_timeout_ms": 100}))
    try:
        with pytest.raises(ModelProviderError) as queue_error:
            await provider.prepare(request, plan=plan)
        assert queue_error.value.code == "model.invocation_cancelled"
        assert queue_error.value.side_effect_state == "not_started"
    finally:
        await held.aclose()

    class SlowClientFactory:
        async def acquire(self, frozen_plan: object) -> object:
            del frozen_plan
            await asyncio.sleep(1)
            raise AssertionError("total deadline 应在 client lease 完成前取消")

    client_provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        client_factory=SlowClientFactory(),  # type: ignore[arg-type]
    )
    with pytest.raises(ModelProviderError) as client_error:
        await client_provider.prepare(request, plan=plan)
    assert client_error.value.code == "model.invocation_cancelled"
    assert client_error.value.side_effect_state == "not_started"

    def slow_agent_factory(_plan: object) -> SequenceAgent:
        """同步 double 模拟昂贵构造，证明返回 prepared 前也复核 total deadline。"""

        time.sleep(0.02)
        return SequenceAgent([ResultDouble()])

    sync_factory_provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=slow_agent_factory,
    )
    with pytest.raises(ModelProviderError) as factory_error:
        await sync_factory_provider.prepare(request, plan=plan)
    assert factory_error.value.code == "model.invocation_cancelled"
    assert factory_error.value.side_effect_state == "not_started"


def test_route_reservation_bound_and_adapter_output_cap_are_enforced_before_send() -> None:
    """route 的调用级 reservation 与 adapter cap 来自同一冻结公式。"""

    model_settings, request, policy = retry_route(classifier=True)
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: SequenceAgent([ResultDouble()]),
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    assert plan.output_token_cap == request.max_output_tokens
    assert plan.reserved_token_bound == plan.per_attempt_token_bound * plan.max_attempts


class _OrderedPrepared:
    """把 client lease/mark/send 顺序转换成可断言事件，不执行网络。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.send_calls = 0

    async def send(self) -> ModelResponse:
        self.events.append("send")
        self.send_calls += 1
        return ModelResponse(
            provider="fake",
            model="fake-basic",
            output_text="ok",
            decision=ModelDecision(action="call", estimated_tokens=1),
            token_usage={"input_tokens": 1, "output_tokens": 1},
        )

    async def aclose(self) -> None:
        self.events.append("release")


class _OrderedProvider:
    provider_id = "fake"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared = _OrderedPrepared(events)

    async def prepare(self, request: ModelRequest, *, plan: object) -> _OrderedPrepared:
        del request, plan
        self.events.extend(["permit", "client"])
        return self.prepared

    async def complete(self, request: ModelRequest, *, plan: object) -> Any:
        raise AssertionError("invocation 必须使用 prepare/send seam")


class _OrderedInvocation(ModelInvocationService):
    """仅插入观察点，实际 reservation/mark 仍委托生产实现。"""

    def __init__(self, *, events: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events = events
        self.cancel_before_mark = False

    async def _start_settlement(self, **kwargs: Any) -> Any:
        self.events.append("reservation")
        return await super()._start_settlement(**kwargs)

    async def _mark_side_effect_started(self, **kwargs: Any) -> None:
        if self.cancel_before_mark:
            raise asyncio.CancelledError
        self.events.append("mark")
        await super()._mark_side_effect_started(**kwargs)


async def _ordered_service(
    tmp_path: Path,
) -> tuple[_OrderedInvocation, _OrderedProvider, str, SQLAlchemyStorage]:
    database = tmp_path / "ordered.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await usage_run(storage)
    events: list[str] = []
    provider = _OrderedProvider(events)

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    service = _OrderedInvocation(
        events=events,
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=LocalJsonlEventSink(tmp_path / "ordered-events.jsonl"),
            run_trace_resolver=resolve_trace,
        ),
    )
    return service, provider, run_id, storage


@pytest.mark.asyncio
async def test_execution_order_and_each_pre_send_failure_boundary_are_fenced(
    tmp_path: Path,
) -> None:
    """公共 complete 必须保持 reservation→permit→client→mark→send。"""

    service, provider, run_id, storage = await _ordered_service(tmp_path)
    try:
        await service.complete(
            ModelRequest(provider="fake", prompt="x", max_output_tokens=1),
            context=UsageEvidenceContext(
                tenant_id="tenant-a", run_id=run_id, agent_id="agent-a", trace_id="trace-a"
            ),
            usage_call_id="ordered-call",
        )
        assert provider.events == [
            "reservation",
            "permit",
            "client",
            "mark",
            "send",
            "release",
        ]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pre_mark_cancel_rolls_back_reservation_permit_and_client_without_network(
    tmp_path: Path,
) -> None:
    """mark 前取消释放 permit/client，send 计数为零并返回稳定取消码。"""

    service, provider, run_id, storage = await _ordered_service(tmp_path)
    service.cancel_before_mark = True
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await service.complete(
                ModelRequest(provider="fake", prompt="x", max_output_tokens=1),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="cancel-before-mark",
            )
        assert exc_info.value.code == "model.invocation_cancelled"
        assert provider.prepared.send_calls == 0
        assert provider.events == ["reservation", "permit", "client", "release"]
    finally:
        await storage.dispose()
