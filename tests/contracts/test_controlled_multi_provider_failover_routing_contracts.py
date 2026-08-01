"""跨 deployment route chain 的公开配置与规划合同。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import ModelDeploymentSettings, SettingsLoadError, load_settings
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
from agent_harness.registry import AgentModelPolicy, AgentRegistry, RegistryLoadError


@dataclass
class _ProviderDouble:
    """只记录不可达的 provider 调用，路由规划本身不得产生副作用。"""

    provider_id: str
    calls: int = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """若规划错误触碰 provider，以计数暴露越界而不访问网络。"""

        self.calls += 1
        return ModelResponse(
            provider=self.provider_id,
            model=plan.model,
            output_text="unexpected",
            decision=ModelDecision(action="call", estimated_tokens=1),
            token_usage={},
        )


ROUTE_A = {"deployment_id": "real_primary", "model_id": "fixture-text-1"}
ROUTE_B = {"deployment_id": "real_secondary", "model_id": "fixture-text-2"}


def two_deployment_override() -> dict[str, object]:
    """构造同 kind 但安全身份完全隔离的两个真实 deployment。"""

    overrides = real_model_override()
    model = cast(dict[str, Any], overrides["model"])
    deployments = cast(dict[str, Any], model["deployments"])
    catalogs = cast(dict[str, Any], model["model_catalogs"])
    secondary_catalog = deepcopy(catalogs["fixture_text_1"])
    secondary_catalog.update(
        {
            "model": "fixture-text-2",
            "price_source_ref": "fixture-price-secondary",
        }
    )
    secondary_catalog["digest"] = model_catalog_digest("fixture_text_2", secondary_catalog)
    catalogs["fixture_text_2"] = secondary_catalog

    model["credentials"]["real_secondary_key"] = {
        "value": "controlled-failover-secondary-secret-fixture",
        "allowed_origins": ["https://models-secondary.example.test"],
    }
    model["endpoint_policies"]["real_secondary_endpoint"] = {
        "version": "v1",
        "provider_kind": "openai-compatible",
        "allowed_origins": ["https://models-secondary.example.test"],
        "completion_classifiers": [],
    }
    secondary = deepcopy(deployments["real_primary"])
    secondary.update(
        {
            "allowed_models": ["fixture-text-2"],
            "model_catalog_refs": {"fixture-text-2": "fixture_text_2"},
            "model_catalog_versions": {"fixture-text-2": "v1"},
            "default_model": "fixture-text-2",
            "base_url": "https://models-secondary.example.test/v1",
            "endpoint_policy_ref": "real_secondary_endpoint",
            "credential_ref": "real_secondary_key",
        }
    )
    deployments["real_secondary"] = secondary
    return overrides


@pytest.mark.parametrize("status", [400, 401, 402, 404, 499])
def test_cross_provider_failover_statuses_reject_non_contract_4xx(status: int) -> None:
    """跨供应商切换只能接受契约列明的403、429与5xx状态。"""

    overrides = two_deployment_override()
    deployment = cast(dict[str, Any], overrides["model"])["deployments"]["real_primary"]  # type: ignore[index]
    deployment.update(  # type: ignore[union-attr]
        {
            "completion_classifier_ref": "trusted_response_header_not_started",
            "completion_classifier_version": "v1",
            "cross_provider_failover_http_statuses": [status],
        }
    )

    with pytest.raises(ValidationError):
        ModelDeploymentSettings.model_validate(deployment)


def test_cross_provider_failover_statuses_sort_and_deduplicate_contract_whitelist() -> None:
    """合法白名单按稳定顺序冻结，重复项不能改变route identity。"""

    overrides = two_deployment_override()
    deployment = cast(dict[str, Any], overrides["model"])["deployments"]["real_primary"]  # type: ignore[index]
    deployment.update(  # type: ignore[union-attr]
        {
            "completion_classifier_ref": "trusted_response_header_not_started",
            "completion_classifier_version": "v1",
            "cross_provider_failover_http_statuses": [599, 403, 429, 500, 403],
        }
    )

    validated = ModelDeploymentSettings.model_validate(deployment)

    assert validated.cross_provider_failover_http_statuses == [403, 429, 500, 599]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completion_classifier_ref", "arbitrary_classifier"),
        ("completion_classifier_version", "v9"),
    ],
)
def test_deployment_rejects_unsupported_completion_classifier_identity(
    field: str,
    value: str,
) -> None:
    """deployment 只能引用 runtime 实际实现的唯一 classifier identity。"""

    overrides = two_deployment_override()
    deployment = cast(dict[str, Any], overrides["model"])["deployments"]["real_primary"]  # type: ignore[index]
    deployment.update(  # type: ignore[union-attr]
        {
            "completion_classifier_ref": "trusted_response_header_not_started",
            "completion_classifier_version": "v1",
            "cross_provider_failover_http_statuses": [500],
            field: value,
        }
    )

    with pytest.raises(ValidationError):
        ModelDeploymentSettings.model_validate(deployment)


def test_model_settings_reject_endpoint_policy_with_unsupported_completion_classifier() -> None:
    """endpoint policy 不能把 runtime 不支持的 classifier 伪装成受信配置。"""

    overrides = two_deployment_override()
    model = cast(dict[str, Any], overrides["model"])
    deployments = cast(dict[str, Any], model["deployments"])
    policies = cast(dict[str, Any], model["endpoint_policies"])
    for deployment_id in ("real_primary", "real_secondary"):
        deployment = cast(dict[str, Any], deployments[deployment_id])
        deployment.update(
            {
                "completion_classifier_ref": "arbitrary_classifier",
                "completion_classifier_version": "v9",
                "cross_provider_failover_http_statuses": [500],
            }
        )
        policy = cast(dict[str, Any], policies[str(deployment["endpoint_policy_ref"])])
        policy["completion_classifiers"] = [{"ref": "arbitrary_classifier", "version": "v9"}]

    with pytest.raises(SettingsLoadError):
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def _chain_policy_payload() -> dict[str, object]:
    """A 兼容投影只服务旧读者，完整授权以有序 route refs 表达。"""

    return {
        "deployment_id": "real_primary",
        "provider": "openai-compatible",
        "allowed_models": ["fixture-text-1"],
        "default_model": "fixture-text-1",
        "fallback_models": [],
        "fallback_routes": [ROUTE_A, ROUTE_B],
    }


def _router() -> tuple[ModelRouter, _ProviderDouble, _ProviderDouble]:
    """返回两个隔离 provider double；所有 AC-090 路径都必须保持零调用。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=two_deployment_override(),
    )
    provider_a = _ProviderDouble(provider_id="openai-compatible")
    provider_b = _ProviderDouble(provider_id="openai-compatible")
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={
            "openai-compatible": provider_a,
            # 第二个 double 以 deployment identity 暴露给后续 composition；当前
            # planner 仍按 provider kind 查 façade，两个对象都必须保持零调用。
            "real_secondary": provider_b,
        },
        model_settings=settings.model,
    )
    return router, provider_a, provider_b


def _write_chain_agent(root: Path, *, valid_projection: bool, marker: Path) -> None:
    """写入最小 registry 包，验证 YAML summary 在 executor import 前完成校验。"""

    package = root / "chain"
    package.mkdir(parents=True)
    projected_deployment = "real_primary" if valid_projection else "real_secondary"
    projected_model = "fixture-text-1" if valid_projection else "fixture-text-2"
    package.joinpath("config.yaml").write_text(
        f"""agent_id: examples.chain
version: 0.1.0
name: chain
description: Route chain registry fixture.
input_schema: agents.chain.schemas.Input
output_schema: agents.chain.schemas.Output
executor: executor:executor
model:
  deployment_id: {projected_deployment}
  provider: openai-compatible
  allowed_models: [{projected_model}]
  default_model: {projected_model}
  fallback_models: []
  fallback_routes:
    - deployment_id: real_primary
      model_id: fixture-text-1
    - deployment_id: real_secondary
      model_id: fixture-text-2
budget:
  max_tokens_per_run: 128
  max_cost_usd_per_run: 1.0
tool_allowlist: []
delegation_edges: []
""",
        encoding="utf-8",
    )
    package.joinpath("schemas.py").write_text(
        """from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    ok: bool = True
""",
        encoding="utf-8",
    )
    package.joinpath("executor.py").write_text(
        f"""from pathlib import Path
from agent_harness.runtime import AgentExecutionResult

Path({str(marker)!r}).write_text("imported", encoding="utf-8")

class Executor:
    async def run(self, request, context):
        return AgentExecutionResult.completed({{"ok": True}})

    async def resume(self, request, context, grant):
        return AgentExecutionResult.completed({{"ok": True}})

executor = Executor()
""",
        encoding="utf-8",
    )


def test_registry_loader_preserves_full_route_summary(tmp_path: Path) -> None:
    """YAML loader 必须把完整授权公开给 summary，而不是只留下 legacy 投影。"""

    root = tmp_path / "agents"
    marker = tmp_path / "executor-imported.txt"
    _write_chain_agent(root, valid_projection=True, marker=marker)

    registry = AgentRegistry.load_from_directory(root)
    policy = registry.get("examples.chain").model_policy

    assert policy.deployment_id == "real_primary"
    assert [route.model_dump(mode="json") for route in policy.fallback_routes] == [
        ROUTE_A,
        ROUTE_B,
    ]
    assert marker.exists()


def test_registry_loader_rejects_projection_rewrite_before_executor_import(
    tmp_path: Path,
) -> None:
    """把兼容投影改成 B 必须整体拒绝，不能先导入任意 executor。"""

    root = tmp_path / "agents"
    marker = tmp_path / "executor-imported.txt"
    _write_chain_agent(root, valid_projection=False, marker=marker)

    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(root)

    assert not marker.exists()


def test_agent_route_chain_preserves_a_projection_and_full_authorization() -> None:
    """Agent `[A,B]` 的旧字段必须严格投影 A，后继只能由 route refs 授权。"""

    policy = AgentModelPolicy.model_validate(_chain_policy_payload())

    assert policy.deployment_id == "real_primary"
    assert policy.provider == "openai-compatible"
    assert policy.allowed_models == ["fixture-text-1"]
    assert policy.default_model == "fixture-text-1"
    assert policy.fallback_models == []
    assert [route.model_dump(mode="json") for route in policy.fallback_routes] == [
        ROUTE_A,
        ROUTE_B,
    ]

    invalid_projection = _chain_policy_payload() | {
        "deployment_id": "real_secondary",
        "allowed_models": ["fixture-text-2"],
        "default_model": "fixture-text-2",
    }
    with pytest.raises(ValidationError):
        AgentModelPolicy.model_validate(invalid_projection)


def test_model_request_accepts_only_route_ref_identity_fields() -> None:
    """合法 route_refs 必须在公共请求 DTO 成形，且不得携带私有安全配置。"""

    request = ModelRequest.model_validate(
        {
            "prompt": "hello",
            "max_output_tokens": 8,
            "route_refs": [ROUTE_B],
        }
    )

    assert request.route_refs is not None
    assert [route.model_dump(mode="json") for route in request.route_refs] == [ROUTE_B]


def test_request_b_only_keeps_agent_a_projection_and_builds_v1_single_candidate_chain() -> None:
    """Request `[B]` 只改变 candidates，不得把原 Agent identity 改写成 B。"""

    router, provider_a, provider_b = _router()
    request = ModelRequest.model_validate(
        {
            "prompt": "hello",
            "max_output_tokens": 8,
            "route_refs": [ROUTE_B],
        }
    )
    policy = AgentModelPolicy.model_validate(_chain_policy_payload())

    chain = router.plan_chain(request, agent_policy=policy)
    payload = chain.model_dump(mode="json")

    assert chain.schema_version == "model-route-chain-v1"
    assert chain.candidate_count == 1
    assert [(item.deployment_id, item.model) for item in chain.candidates] == [
        ("real_secondary", "fixture-text-2")
    ]
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["ordinal"] == 1
    assert policy.deployment_id == "real_primary"
    assert [route.model_dump(mode="json") for route in policy.fallback_routes] == [
        ROUTE_A,
        ROUTE_B,
    ]
    assert "base_url" not in repr(payload)
    assert payload["candidates"][0]["credential_ref"] == "real_secondary_key"
    assert "controlled-failover-secondary-secret-fixture" not in repr(payload)
    assert provider_a.calls == provider_b.calls == 0


def test_omitted_request_route_refs_freezes_full_agent_order() -> None:
    """Request 未缩权时按 Agent `[A,B]` 原顺序冻结，不能按 provider map 重排。"""

    router, provider_a, provider_b = _router()
    policy = AgentModelPolicy.model_validate(_chain_policy_payload())
    request = ModelRequest(prompt="hello", max_output_tokens=8)

    chain = router.plan_chain(request, agent_policy=policy)

    assert chain.schema_version == "model-route-chain-v1"
    assert chain.candidate_count == 2
    assert [
        (item.ordinal, item.deployment_id, item.provider, item.model) for item in chain.candidates
    ] == [
        (1, "real_primary", "openai-compatible", "fixture-text-1"),
        (2, "real_secondary", "openai-compatible", "fixture-text-2"),
    ]
    assert provider_a.calls == provider_b.calls == 0


def test_router_exposes_explicit_plan_chain_seam() -> None:
    """显式 chain 必须有独立 planner，不能把首候选塞回 legacy `plan()`。"""

    router, provider_a, provider_b = _router()

    assert callable(router.plan_chain)
    assert provider_a.calls == provider_b.calls == 0


def test_request_route_refs_are_a_nonempty_ordered_subsequence() -> None:
    """Request 只能删除候选，不能插入、重复或重排 Agent 最大授权集。"""

    router, provider_a, provider_b = _router()
    policy = AgentModelPolicy.model_validate(_chain_policy_payload())
    invalid_route_refs = [
        [],
        [ROUTE_B, ROUTE_A],
        [ROUTE_A, ROUTE_A],
        [ROUTE_A, {"deployment_id": "unknown", "model_id": "unknown"}],
    ]

    for route_refs in invalid_route_refs:
        request = ModelRequest.model_validate(
            {
                "prompt": "hello",
                "max_output_tokens": 8,
                "route_refs": route_refs,
            }
        )
        with pytest.raises(ModelRouteError) as exc_info:
            router.plan_chain(request, agent_policy=policy)
        assert exc_info.value.code == "model.route_not_allowed"

    assert provider_a.calls == provider_b.calls == 0


@pytest.mark.parametrize("forbidden", ["endpoint", "credential_ref"])
def test_request_cannot_inject_candidate_security_identity(forbidden: str) -> None:
    """endpoint 与 credential 永远来自 deployment，公共请求 DTO 必须前置拒绝。"""

    with pytest.raises(ValidationError) as exc_info:
        ModelRequest.model_validate(
            {
                "prompt": "hello",
                "max_output_tokens": 8,
                "route_refs": [ROUTE_A],
                forbidden: "attacker-controlled",
            }
        )

    assert {tuple(error["loc"]) for error in exc_info.value.errors()} == {(forbidden,)}


def test_legacy_descriptor_does_not_implicitly_enable_route_chain() -> None:
    """缺少 fallback_routes 的旧 descriptor 仍只返回 legacy `ModelRoutePlan`。"""

    router, provider_a, provider_b = _router()
    legacy_policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="hello",
        max_output_tokens=8,
    )

    plan = router.plan(request, agent_policy=legacy_policy)

    assert isinstance(plan, ModelRoutePlan)
    assert plan.deployment_id == "real_primary"
    assert not hasattr(legacy_policy, "fallback_routes") or not legacy_policy.fallback_routes
    assert provider_a.calls == provider_b.calls == 0
