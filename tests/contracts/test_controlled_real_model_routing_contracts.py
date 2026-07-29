"""deployment、Agent 与请求三层只缩权的公共路由合同。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import load_settings
from agent_harness.models import (
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.models.router import ModelRouteError
from agent_harness.registry import AgentModelPolicy


@dataclass
class AsyncProviderDouble:
    """只记录收到的冻结 provider/model/cap，不接触 SDK 或网络。"""

    provider_id: str = "openai-compatible"
    calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """返回固定 usage，证明 execute 消费 route plan 而非重选 route。"""

        self.calls += 1
        model = plan.model
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text="controlled-result",
            decision=ModelDecision(action="call", estimated_tokens=1),
            token_usage={"input_tokens": 1, "output_tokens": 1},
        )


def _router() -> tuple[ModelRouter, AsyncProviderDouble, AgentModelPolicy]:
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    provider = AsyncProviderDouble()
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    return router, provider, policy


def test_route_plan_intersects_deployment_agent_and_request_before_side_effects() -> None:
    """request 只能从 deployment∩Agent 中选模型，非法值不触发 provider。"""

    router, provider, policy = _router()
    plan = router.plan(
        ModelRequest(
            deployment_id="real_primary",
            provider="openai-compatible",
            model="fixture-text-1",
            prompt="hello",
            max_output_tokens=32,
        ),
        agent_policy=policy,
    )

    assert plan.deployment_id == "real_primary"
    assert plan.allowed_models == ("fixture-text-1",)
    assert plan.output_token_cap == 32
    assert plan.trusted_input_token_bound == len(b"hello") + 16
    assert provider.calls == 0

    with pytest.raises(ModelRouteError) as exc_info:
        router.plan(
            ModelRequest(
                deployment_id="real_primary",
                provider="openai-compatible",
                model="outside-agent-policy",
                prompt="hello",
                max_output_tokens=1,
            ),
            agent_policy=policy,
        )
    assert exc_info.value.code == "model.route_not_allowed"
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["plan", "route"])
async def test_controlled_router_requires_agent_policy_before_provider(
    entrypoint: str,
) -> None:
    """受控 settings 缺 Agent policy 时必须拒绝，不能回退 legacy 路由绕过交集。"""

    router, provider, _policy = _router()
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="hello",
        max_output_tokens=1,
    )

    with pytest.raises(ModelRouteError) as exc_info:
        if entrypoint == "plan":
            router.plan(request)
        else:
            await router.route(request)

    assert exc_info.value.code == "model.route_not_allowed"
    assert provider.calls == 0


def test_provider_identity_assertion_matches_deployment_kind_and_bound_adapter() -> None:
    """公共 provider identity 始终是 openai-compatible，私有 adapter 名不得参与路由。"""

    router, provider, policy = _router()
    with pytest.raises(ModelRouteError) as exc_info:
        router.plan(
            ModelRequest(
                deployment_id="real_primary",
                provider="pydantic-ai",
                prompt="hello",
                max_output_tokens=1,
            ),
            agent_policy=policy,
        )
    assert exc_info.value.code == "model.route_not_allowed"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_route_plan_is_immutable_and_execute_uses_frozen_identity() -> None:
    """plan 形成后自身嵌套策略和外部 request/policy 都不能改写执行边界。"""

    router, provider, policy = _router()
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        prompt="hello",
        max_output_tokens=8,
    )
    plan = router.plan(request, agent_policy=policy)

    with pytest.raises(ValidationError):
        plan.decision.action = "policy_required"
    with pytest.raises(ValidationError):
        plan.retry_policy.max_attempts = 99
    with pytest.raises(ValidationError):
        plan.bulkhead_policy.max_in_flight = 99

    request.max_output_tokens = 64
    policy.allowed_models.clear()

    response = await router.execute(request, plan=plan)

    assert response.provider == plan.provider == "openai-compatible"
    assert response.model == plan.model == "fixture-text-1"
    assert plan.output_token_cap == 8
    assert plan.retry_policy.max_attempts == plan.max_attempts
    assert plan.bulkhead_policy.max_in_flight == 2
    assert provider.calls == 1


def test_missing_price_credential_or_capability_rejects_before_provider() -> None:
    """能力、credential 与价格目录缺失都必须在 provider/client 前拒绝。"""

    router, provider, policy = _router()
    with pytest.raises(ModelRouteError) as exc_info:
        router.plan(
            ModelRequest(
                deployment_id="real_primary",
                provider="openai-compatible",
                capability="structured_output",
                prompt="hello",
                max_output_tokens=1,
            ),
            agent_policy=policy,
        )
    assert exc_info.value.code == "model.capability_unsupported"
    assert provider.calls == 0


def test_request_dto_rejects_endpoint_or_credential_override() -> None:
    """endpoint/credential 不属于公共请求 DTO，原始输入在 router 前即被拒绝。"""

    with pytest.raises(ValidationError):
        ModelRequest.model_validate(
            {
                "prompt": "hello",
                "max_output_tokens": 1,
                "endpoint": "https://evil.example.test",
                "credential_ref": "evil",
            }
        )
