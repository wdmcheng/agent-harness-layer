"""受控真实模型 fallback 与 policy-needed 的公共路由合同。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pytest
from tests.contracts.provider_neutral_structured_output_test_support import (
    fixture_output_schema_identity,
)
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import load_settings
from agent_harness.config.model_catalog import model_catalog_digest
from agent_harness.models import (
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.models.router import ModelRouteError
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime


@dataclass
class _ProviderDouble:
    """只暴露绑定 identity；route 红灯不得走到 provider。"""

    provider_id: str = "openai-compatible"
    calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            provider=self.provider_id,
            model=plan.model,
            output_text="unexpected",
            decision=ModelDecision(action="call", estimated_tokens=1),
            token_usage={},
        )


def _two_model_override() -> dict[str, object]:
    """构造默认模型较贵、fallback 较小的完整 typed deployment。"""

    overrides = real_model_override()
    model = cast(dict[str, Any], overrides["model"])
    fallback_catalog: dict[str, object] = {
        "version": "v1",
        "provider_kind": "openai-compatible",
        "model": "fixture-text-small",
        "request_shape_ref": "single-user-text-no-tools",
        "request_shape_version": "v1",
        "input_bound_strategy_ref": "utf8-bytes-plus-envelope",
        "input_bound_strategy_version": "v1",
        "input_envelope_token_bound": 4,
        "cost_enabled": True,
        "input_token_price_usd": "0.0000001",
        "output_token_price_usd": "0.0000002",
        "price_source_ref": "fixture-price-small",
        "price_source_version": "v1",
    }
    fallback_catalog["digest"] = model_catalog_digest("fixture_text_small", fallback_catalog)
    cast(dict[str, object], model["model_catalogs"])["fixture_text_small"] = fallback_catalog
    deployment = cast(dict[str, Any], cast(dict[str, object], model["deployments"])["real_primary"])
    deployment["allowed_models"] = ["fixture-text-1", "fixture-text-small"]
    deployment["fallback_models"] = ["fixture-text-small"]
    deployment["model_catalog_refs"]["fixture-text-small"] = "fixture_text_small"
    deployment["model_catalog_versions"]["fixture-text-small"] = "v1"
    return overrides


def _policy() -> AgentModelPolicy:
    """Agent fallback 顺序必须与 deployment 共同缩权。"""

    return AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1", "fixture-text-small"],
        default_model="fixture-text-1",
        fallback_models=["fixture-text-small"],
    )


def _request() -> ModelRequest:
    """默认 route 超过 80 token，small route 仍在阈值内。"""

    return ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        prompt="0123456789",
        max_output_tokens=64,
    )


def test_controlled_route_uses_only_frozen_intersection_fallback_and_recomputes_bounds() -> None:
    """默认 route 超过 token 阈值时，fallback 必须重算目录、价格与 reservation。"""

    settings = load_settings(
        profile="local", profiles_dir=PROFILES, overrides=_two_model_override()
    )
    provider = _ProviderDouble()
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
            max_tokens_per_call=80,
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )

    plan = router.plan(_request(), agent_policy=_policy())

    assert plan.model == "fixture-text-small"
    assert plan.decision.action == "fallback"
    assert plan.decision.fallback_model == "fixture-text-small"
    assert plan.decision.estimated_tokens == 78
    assert plan.decision.max_tokens == 80
    assert plan.trusted_input_token_bound == 14
    assert plan.per_attempt_token_bound == 78
    assert plan.model_catalog_ref == "fixture_text_small"
    assert plan.price_source_ref == "fixture-price-small"
    assert provider.calls == 0


def test_controlled_route_cost_threshold_falls_back_and_exposes_safe_decision_summary() -> None:
    """Cost 超阈值也必须选择较小 route，并在 decision 中保留估算与阈值。"""

    settings = load_settings(
        profile="local", profiles_dir=PROFILES, overrides=_two_model_override()
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
            max_cost_per_call=Decimal("0.0001000"),
        ),
        providers={"openai-compatible": _ProviderDouble()},
        model_settings=settings.model,
    )

    plan = router.plan(_request(), agent_policy=_policy())

    assert plan.model == "fixture-text-small"
    assert plan.decision.action == "fallback"
    assert plan.decision.estimated_cost_usd == Decimal("0.0000142")
    assert plan.decision.max_cost_usd == Decimal("0.0001000")


def test_controlled_fallback_cannot_expand_agent_or_deployment_frozen_intersection() -> None:
    """任一配置层未声明 fallback 时不得借另一层的列表自动扩权。"""

    overrides = _two_model_override()
    model = cast(dict[str, Any], overrides["model"])
    deployment = cast(dict[str, Any], cast(dict[str, object], model["deployments"])["real_primary"])
    deployment["fallback_models"] = []
    settings = load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
            max_tokens_per_call=80,
        ),
        providers={"openai-compatible": _ProviderDouble()},
        model_settings=settings.model,
    )

    deployment_denied = router.plan(_request(), agent_policy=_policy())
    agent_policy = _policy()
    agent_policy.fallback_models = []
    agent_denied = ModelRouter(
        config=router.config,
        providers={"openai-compatible": _ProviderDouble()},
        model_settings=load_settings(
            profile="local", profiles_dir=PROFILES, overrides=_two_model_override()
        ).model,
    ).plan(_request(), agent_policy=agent_policy)

    assert deployment_denied.decision.action == "policy_required"
    assert agent_denied.decision.action == "policy_required"


def test_explicit_request_model_remains_exact_and_does_not_enable_automatic_fallback() -> None:
    """request 已明确选模时 fallback 会扩大 intent，因此只能返回 policy-needed。"""

    settings = load_settings(
        profile="local", profiles_dir=PROFILES, overrides=_two_model_override()
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
            max_tokens_per_call=80,
        ),
        providers={"openai-compatible": _ProviderDouble()},
        model_settings=settings.model,
    )
    explicit = _request().model_copy(update={"model": "fixture-text-1"})

    plan = router.plan(explicit, agent_policy=_policy())

    assert plan.model == "fixture-text-1"
    assert plan.decision.action == "policy_required"


def _snapshot(*, max_tokens: int, max_cost: Decimal) -> tuple[ModelRouter, dict[str, Any]]:
    """冻结带双模型 route 与 target hard budget 的真实 v2 快照。"""

    settings = load_settings(
        profile="local", profiles_dir=PROFILES, overrides=_two_model_override()
    )
    policy = _policy()
    registry = AgentRegistry(
        [
            AgentDescriptor(
                agent_id="agent-real",
                version="v1",
                name="真实 fallback Agent",
                description="仅用于冻结路由合同",
                input_schema_ref="fixture.Input",
                output_schema_ref="fixture.Output",
                output_schema_identity=fixture_output_schema_identity(),
                config_ref="fixture/config.yaml",
                tool_policy=AgentToolPolicy(allowed_tools=[]),
                model_policy=policy,
                budget=AgentBudget(
                    max_tokens_per_run=max_tokens,
                    max_cost_usd_per_run=float(max_cost),
                ),
                eval_dataset=None,
                delegation_targets=[],
            )
        ]
    )
    ledger = SharedBudgetRuntime(settings=settings, registry=registry).ledger_create(
        tenant_id="tenant-a", run_id="run-a", agent_id="agent-real"
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible", default_model="fixture-text-1"
        ),
        providers={"openai-compatible": _ProviderDouble()},
        model_settings=settings.model,
    )
    return router, ledger.snapshot


def test_frozen_route_fallback_rechecks_target_hard_budget_and_catalog_identity() -> None:
    """恢复只能使用冻结 fallback，并以该 route 的目录重算 hard reservation。"""

    router, snapshot = _snapshot(max_tokens=80, max_cost=Decimal("0.0001000"))

    plan = router.plan_from_snapshot(_request(), snapshot=snapshot, agent_id="agent-real")

    assert plan.model == "fixture-text-small"
    assert plan.decision.action == "fallback"
    assert plan.reserved_token_bound == 78
    assert plan.reserved_cost_bound == Decimal("0.0000142")
    assert plan.model_catalog_ref == "fixture_text_small"


def test_frozen_route_returns_policy_needed_when_every_fallback_exceeds_hard_budget() -> None:
    """没有满足 frozen hard limit 的候选时只返回 policy-needed，禁止静默调用默认模型。"""

    router, snapshot = _snapshot(max_tokens=20, max_cost=Decimal("0.000001"))

    plan = router.plan_from_snapshot(_request(), snapshot=snapshot, agent_id="agent-real")

    assert plan.model == "fixture-text-1"
    assert plan.decision.action == "policy_required"
    assert plan.decision.fallback_model is None
    assert plan.decision.reason == "estimated budget exceeds threshold and no fallback is eligible"


def test_corrupt_frozen_fallback_formula_fails_closed_instead_of_becoming_policy_decision() -> None:
    """候选 route 的冻结公式损坏属于快照错误，不得降级成可批准 soft decision。"""

    router, original = _snapshot(max_tokens=80, max_cost=Decimal("0.0001000"))
    snapshot = deepcopy(original)
    routes = snapshot["agents"]["agent-real"]["routes"]
    fallback = next(item for item in routes if item["model"] == "fixture-text-small")
    fallback["max_per_attempt_token_bound"] += 1

    with pytest.raises(ModelRouteError) as exc_info:
        router.plan_from_snapshot(_request(), snapshot=snapshot, agent_id="agent-real")

    assert exc_info.value.code == "budget.reservation_rejected"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_per_attempt_cost_bound", "0.123"),
        ("capabilities", []),
    ],
)
def test_corrupt_frozen_fallback_price_or_capability_fails_before_policy(
    field: str, value: object
) -> None:
    """候选价格公式和 capability 都属于 hard eligibility，不能被 soft fallback 跳过。"""

    router, original = _snapshot(max_tokens=80, max_cost=Decimal("0.0001000"))
    snapshot = deepcopy(original)
    routes = snapshot["agents"]["agent-real"]["routes"]
    fallback = next(item for item in routes if item["model"] == "fixture-text-small")
    fallback[field] = value

    with pytest.raises(ModelRouteError) as exc_info:
        router.plan_from_snapshot(_request(), snapshot=snapshot, agent_id="agent-real")

    assert exc_info.value.code in {
        "budget.reservation_rejected",
        "model.capability_unsupported",
    }


def test_router_marks_only_soft_threshold_as_approval_eligible() -> None:
    """Router 只描述可审批 soft gate；hard limit 不产生可伪造的批准入口。"""

    settings = load_settings(
        profile="local", profiles_dir=PROFILES, overrides=_two_model_override()
    )
    direct = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
            max_tokens_per_call=80,
        ),
        providers={"openai-compatible": _ProviderDouble()},
        model_settings=settings.model,
    )
    # 显式 model 冻结了原 intent，Router 不得先替换为 fallback 再让审批恢复。
    soft_gate = direct.plan(
        _request().model_copy(update={"model": "fixture-text-1"}),
        agent_policy=_policy(),
    )
    frozen, snapshot = _snapshot(max_tokens=20, max_cost=Decimal("0.000001"))
    hard_gate = frozen.plan_from_snapshot(_request(), snapshot=snapshot, agent_id="agent-real")

    assert soft_gate.model == "fixture-text-1"
    assert soft_gate.decision.action == "policy_required"
    assert soft_gate.approval_kind == "soft_budget"
    assert hard_gate.decision.action == "policy_required"
    assert hard_gate.approval_kind is None
