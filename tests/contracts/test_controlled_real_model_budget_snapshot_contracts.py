"""受控真实模型共享预算快照版本合同。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import load_settings
from agent_harness.models import ModelRequest, ModelRouter, ModelRouterConfig
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage._shared_budget_repository_records import (
    _ledger_create_snapshot_valid,
)


def _registry(*, provider: str = "openai-compatible") -> AgentRegistry:
    """构造只声明真实 deployment 缩权集合的最小 registry。"""

    return AgentRegistry(
        [
            AgentDescriptor(
                agent_id="agent-real",
                version="v1",
                name="真实文本 Agent",
                description="仅用于离线快照合同",
                input_schema_ref="fixture.Input",
                output_schema_ref="fixture.Output",
                config_ref="fixture/config.yaml",
                tool_policy=AgentToolPolicy(allowed_tools=[]),
                model_policy=AgentModelPolicy(
                    deployment_id="real_primary",
                    provider=provider,
                    allowed_models=["fixture-text-1"],
                    default_model="fixture-text-1",
                    fallback_models=[],
                ),
                budget=AgentBudget(
                    max_tokens_per_run=4096,
                    max_cost_usd_per_run=1.0,
                ),
                eval_dataset=None,
                delegation_targets=[],
            )
        ]
    )


def _fake_registry() -> AgentRegistry:
    """构造与真实 deployment 并存但只显式选择 fake 的离线 root。"""

    descriptor = _registry(provider="fake").get("agent-real")
    descriptor.model_policy.deployment_id = "fake_default"
    descriptor.model_policy.allowed_models = ["fake-local"]
    descriptor.model_policy.default_model = "fake-local"
    return AgentRegistry([descriptor])


def test_budget_tree_v2_repository_validator_freezes_deployment_and_allowed_models() -> None:
    """v2 必须持久化 deployment/allowed models/目录身份，损坏后共用 validator 拒绝。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    runtime = SharedBudgetRuntime(settings=settings, registry=_registry())
    ledger = runtime.ledger_create(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-real",
    )
    root = SimpleNamespace(agent_id="agent-real")

    assert ledger.snapshot_id.startswith("budget-tree-v2:")
    assert ledger.snapshot["schema_version"] == "budget-tree-v2"
    policy = ledger.snapshot["agents"]["agent-real"]["model_policy"]
    route = ledger.snapshot["agents"]["agent-real"]["routes"][0]
    assert policy["deployment_id"] == "real_primary"
    assert policy["allowed_models"] == ["fixture-text-1"]
    assert route["model_catalog_digest"]
    assert route["canonical_base_url"] == "https://models.example.test/v1"
    assert route["endpoint_policy_digest"]
    assert route["completion_classifier_ref"] is None
    assert route["completion_classifier_version"] is None
    assert route["retry_policy"]["retryable_http_statuses"] == []
    assert route["bulkhead_policy"]["scope"] == "process_deployment"
    assert route["max_attempts"] == 1
    assert route["max_per_attempt_token_bound"] == 1168
    assert _ledger_create_snapshot_valid(ledger, root) is True  # type: ignore[arg-type]

    for mutation in ("deployment_id", "allowed_models", "model_catalog_digest"):
        snapshot = deepcopy(ledger.snapshot)
        if mutation == "allowed_models":
            snapshot["agents"]["agent-real"]["model_policy"][mutation] = []
        elif mutation == "deployment_id":
            snapshot["agents"]["agent-real"]["model_policy"][mutation] = ""
        else:
            snapshot["agents"]["agent-real"]["routes"][0][mutation] = ""
        corrupted = ledger.model_copy(update={"snapshot": snapshot})
        assert _ledger_create_snapshot_valid(corrupted, root) is False  # type: ignore[arg-type]


def test_budget_tree_v1_rejects_real_provider_without_current_config_projection() -> None:
    """旧 v1 未冻结真实 identity，repository 不得按当前配置补齐后接受。"""

    settings = load_settings(profile="local", profiles_dir=PROFILES)
    runtime = SharedBudgetRuntime(settings=settings, registry=_registry(provider="fake"))
    # fixture 只为制造完整 v1 形状，随后改成真实 provider，证明 fail closed。
    runtime._registry.get("agent-real").model_policy.provider = "fake"  # type: ignore[attr-defined]
    runtime._registry.get("agent-real").model_policy.deployment_id = "fake_default"  # type: ignore[attr-defined]
    runtime._registry.get("agent-real").model_policy.allowed_models = ["fake-local"]  # type: ignore[attr-defined]
    runtime._registry.get("agent-real").model_policy.default_model = "fake-local"  # type: ignore[attr-defined]
    ledger = runtime.ledger_create(tenant_id="tenant-a", run_id="run-a", agent_id="agent-real")
    snapshot = deepcopy(ledger.snapshot)
    snapshot["agents"]["agent-real"]["model_policy"]["provider"] = "openai-compatible"
    snapshot["agents"]["agent-real"]["routes"][0]["provider"] = "openai-compatible"
    corrupt = ledger.model_copy(update={"snapshot": snapshot})

    assert (
        _ledger_create_snapshot_valid(  # type: ignore[arg-type]
            corrupt, cast(Any, SimpleNamespace(agent_id="agent-real"))
        )
        is False
    )


def test_unused_real_deployment_does_not_upgrade_explicit_fake_root_snapshot() -> None:
    """真实配置仅存在但未被 root 引用时，fake 仍使用完全离线的 v1 快照。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    ledger = SharedBudgetRuntime(settings=settings, registry=_fake_registry()).ledger_create(
        tenant_id="tenant-a",
        run_id="run-fake",
        agent_id="agent-real",
    )

    assert ledger.snapshot_id.startswith("budget-tree-v1:")
    assert ledger.snapshot["schema_version"] == "budget-tree-v1"
    route = ledger.snapshot["agents"]["agent-real"]["routes"][0]
    assert route["provider"] == "fake"
    assert "canonical_base_url" not in route


def test_budget_tree_v2_route_restores_old_path_without_current_settings_projection() -> None:
    """旧 run 必须从 v2 快照恢复原 path，reload 后 current deployment 不能改写路由。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    registry = _registry()
    runtime = SharedBudgetRuntime(settings=settings, registry=registry)
    ledger = runtime.ledger_create(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-real",
    )
    settings.model.deployments["real_primary"].base_url = "https://models.example.test/v1/reloaded"
    provider = SimpleNamespace(provider_id="openai-compatible")
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": cast(Any, provider)},
        model_settings=settings.model,
    )

    plan = router.plan_from_snapshot(
        ModelRequest(
            deployment_id="real_primary",
            provider="openai-compatible",
            model="fixture-text-1",
            prompt="hello",
            max_output_tokens=8,
        ),
        snapshot=ledger.snapshot,
        agent_id="agent-real",
    )

    assert plan.canonical_base_url == "https://models.example.test/v1"
    assert (
        plan.endpoint_policy_digest
        == ledger.snapshot["agents"]["agent-real"]["routes"][0]["endpoint_policy_digest"]
    )
