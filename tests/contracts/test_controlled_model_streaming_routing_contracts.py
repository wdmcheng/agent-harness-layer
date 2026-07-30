"""受控模型文本流的当前路由、快照恢复与 prepare 合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from tests.contracts.test_controlled_real_model_budget_snapshot_contracts import _registry
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import load_settings
from agent_harness.models import (
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    ModelStreamCloseResult,
    ModelStreamDelta,
    PreparedModelStreamCall,
)
from agent_harness.registry import AgentModelPolicy
from agent_harness.runtime.shared_budget import SharedBudgetRuntime


class _PreparedStreamDouble:
    """prepare 后保持惰性，只有调用方迭代时才记录首次流副作用。"""

    def __init__(self) -> None:
        self.iterations = 0

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def generate():  # type: ignore[no-untyped-def]
            self.iterations += 1
            yield ModelStreamDelta(text="hello")

        return generate()

    async def result(self) -> ModelResponse:
        return ModelResponse(
            provider="openai-compatible",
            model="fixture-text-1",
            output_text="hello",
            decision=ModelDecision(action="call", estimated_tokens=2),
            token_usage={"input_tokens": 1, "output_tokens": 1},
        )

    async def aclose(self) -> ModelStreamCloseResult:
        return ModelStreamCloseResult(state="not_started", usage=None)


@dataclass
class _StreamingProviderDouble:
    """分别记录 prepare、stream iteration 与一次性调用，证明禁止伪流 fallback。"""

    provider_id: str = "openai-compatible"
    prepare_calls: int = 0
    complete_calls: int = 0
    prepared: _PreparedStreamDouble = field(default_factory=_PreparedStreamDouble)

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelStreamCall:
        assert request.capability == plan.capability == "text_stream"
        self.prepare_calls += 1
        return self.prepared

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        self.complete_calls += 1
        raise AssertionError("text_stream must not fall back to complete")


@dataclass
class _CompletionOnlyProvider:
    """只支持非流式 completion 的 provider，用于副作用前不支持路径。"""

    provider_id: str = "openai-compatible"
    complete_calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        del request, plan
        self.complete_calls += 1
        raise AssertionError("unsupported stream route must reject before complete")


def _stream_settings() -> Any:
    """在现有受控 deployment 上显式增加 text_stream，不改变默认配置。"""

    overrides = real_model_override()
    model = cast(dict[str, Any], overrides["model"])
    deployments = cast(dict[str, dict[str, Any]], model["deployments"])
    deployments["real_primary"]["capabilities"] = ["text_completion", "text_stream"]
    return load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def _policy() -> AgentModelPolicy:
    return AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )


def _request(*, capability: str = "text_stream") -> ModelRequest:
    return ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        capability=capability,
        prompt="hello",
        max_output_tokens=8,
    )


@pytest.mark.asyncio
async def test_current_router_prepares_stream_without_iteration_or_complete_fallback() -> None:
    """当前配置规划允许精确 text_stream，prepare 本身不开始 provider stream。"""

    settings = _stream_settings()
    provider = _StreamingProviderDouble()
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )

    plan = router.plan(_request(), agent_policy=_policy())
    prepared = await router.prepare_stream(_request(), plan=plan)

    assert prepared is provider.prepared
    assert plan.capability == "text_stream"
    assert provider.prepare_calls == 1
    assert provider.prepared.iterations == 0
    assert provider.complete_calls == 0


@pytest.mark.asyncio
async def test_completion_only_provider_rejects_stream_before_any_provider_call() -> None:
    """deployment 声明流能力但绑定 provider 无独立协议时仍必须零副作用拒绝。"""

    settings = _stream_settings()
    provider = _CompletionOnlyProvider()
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    plan = router.plan(_request(), agent_policy=_policy())

    with pytest.raises(ModelRouteError) as exc_info:
        await router.prepare_stream(_request(), plan=plan)

    assert exc_info.value.code == "model.capability_unsupported"
    assert provider.complete_calls == 0


def test_snapshot_router_restores_exact_stream_capability_and_rejects_tampering() -> None:
    """恢复只消费冻结 capabilities；当前配置或未知 capability 不能补写快照。"""

    settings = _stream_settings()
    ledger = SharedBudgetRuntime(settings=settings, registry=_registry()).ledger_create(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-real",
    )
    provider = _StreamingProviderDouble()
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )

    plan = router.plan_from_snapshot(
        _request(),
        snapshot=ledger.snapshot,
        agent_id="agent-real",
    )
    assert plan.capability == "text_stream"

    tampered = deepcopy(ledger.snapshot)
    tampered["agents"]["agent-real"]["routes"][0]["capabilities"] = ["text_completion"]
    with pytest.raises(ModelRouteError) as exc_info:
        router.plan_from_snapshot(
            _request(),
            snapshot=tampered,
            agent_id="agent-real",
        )
    assert exc_info.value.code == "model.capability_unsupported"
    assert provider.prepare_calls == provider.complete_calls == 0


def test_current_and_snapshot_router_reject_unknown_capability() -> None:
    """只放行 text_completion/text_stream，不能把字符串 capability 变成全开放。"""

    settings = _stream_settings()
    provider = _StreamingProviderDouble()
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )

    with pytest.raises(ModelRouteError) as current_error:
        router.plan(_request(capability="tool_stream"), agent_policy=_policy())
    assert current_error.value.code == "model.capability_unsupported"
    assert provider.prepare_calls == provider.complete_calls == 0
