"""受控多供应商回退测试使用的 typed route-chain 配置支撑。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, cast

from tests.contracts.test_controlled_multi_provider_failover_routing_contracts import (
    PROFILES,
    ROUTE_A,
    ROUTE_B,
    two_deployment_override,
)

from agent_harness.config import HarnessSettings, ModelRouteRef, load_settings
from agent_harness.config.model_catalog import model_catalog_digest
from agent_harness.registry import AgentModelPolicy

ROUTE_C = {"deployment_id": "real_tertiary", "model_id": "fixture-text-3"}
ROUTE_D = {"deployment_id": "real_quaternary", "model_id": "fixture-text-4"}
ROUTE_E = {"deployment_id": "real_quinary", "model_id": "fixture-text-5"}
ROUTE_F = {"deployment_id": "real_senary", "model_id": "fixture-text-6"}
ROUTES = (ROUTE_A, ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_E, ROUTE_F)


def three_deployment_override() -> dict[str, object]:
    """在双真实 deployment 基线上增加第三个完全隔离的真实候选。"""

    overrides = two_deployment_override()
    model = cast(dict[str, Any], overrides["model"])
    catalogs = cast(dict[str, Any], model["model_catalogs"])
    deployments = cast(dict[str, Any], model["deployments"])

    tertiary_catalog = deepcopy(catalogs["fixture_text_2"])
    tertiary_catalog.update(
        {
            "model": "fixture-text-3",
            "price_source_ref": "fixture-price-tertiary",
        }
    )
    tertiary_catalog["digest"] = model_catalog_digest("fixture_text_3", tertiary_catalog)
    catalogs["fixture_text_3"] = tertiary_catalog
    model["credentials"]["real_tertiary_key"] = {
        "value": "controlled-failover-tertiary-secret-fixture",
        "allowed_origins": ["https://models-tertiary.example.test"],
    }
    model["endpoint_policies"]["real_tertiary_endpoint"] = {
        "version": "v1",
        "provider_kind": "openai-compatible",
        "allowed_origins": ["https://models-tertiary.example.test"],
        "completion_classifiers": [{"ref": "trusted_response_header_not_started", "version": "v1"}],
    }
    tertiary = deepcopy(deployments["real_secondary"])
    tertiary.update(
        {
            "allowed_models": ["fixture-text-3"],
            "model_catalog_refs": {"fixture-text-3": "fixture_text_3"},
            "model_catalog_versions": {"fixture-text-3": "v1"},
            "default_model": "fixture-text-3",
            "base_url": "https://models-tertiary.example.test/v1",
            "endpoint_policy_ref": "real_tertiary_endpoint",
            "credential_ref": "real_tertiary_key",
        }
    )
    deployments["real_tertiary"] = tertiary
    return overrides


def six_deployment_override() -> dict[str, object]:
    """扩展为六个隔离 deployment，复现真实 profile 的最大常用链形状。"""

    overrides = three_deployment_override()
    model = cast(dict[str, Any], overrides["model"])
    catalogs = cast(dict[str, Any], model["model_catalogs"])
    deployments = cast(dict[str, Any], model["deployments"])
    credentials = cast(dict[str, Any], model["credentials"])
    endpoint_policies = cast(dict[str, Any], model["endpoint_policies"])

    for ordinal, route in enumerate(ROUTES[3:], start=4):
        deployment_id = str(route["deployment_id"])
        model_id = str(route["model_id"])
        catalog_ref = model_id.replace("-", "_")
        endpoint_ref = f"{deployment_id}_endpoint"
        credential_ref = f"{deployment_id}_key"
        origin = f"https://models-{ordinal}.example.test"

        catalog = deepcopy(catalogs["fixture_text_3"])
        catalog.update(
            {
                "model": model_id,
                "price_source_ref": f"fixture-price-{ordinal}",
            }
        )
        catalog["digest"] = model_catalog_digest(catalog_ref, catalog)
        catalogs[catalog_ref] = catalog
        credentials[credential_ref] = {
            "value": f"controlled-failover-{ordinal}-secret-fixture",
            "allowed_origins": [origin],
        }
        endpoint_policies[endpoint_ref] = {
            "version": "v1",
            "provider_kind": "openai-compatible",
            "allowed_origins": [origin],
            "completion_classifiers": [
                {"ref": "trusted_response_header_not_started", "version": "v1"}
            ],
        }
        deployment = deepcopy(deployments["real_tertiary"])
        deployment.update(
            {
                "allowed_models": [model_id],
                "model_catalog_refs": {model_id: catalog_ref},
                "model_catalog_versions": {model_id: "v1"},
                "default_model": model_id,
                "base_url": f"{origin}/v1",
                "endpoint_policy_ref": endpoint_ref,
                "credential_ref": credential_ref,
            }
        )
        deployments[deployment_id] = deployment
    return overrides


def chain_settings(*, route_count: Literal[2, 3, 6] = 3) -> HarnessSettings:
    """加载全部 typed 安全身份，但不构造 client、socket 或真实 SDK。"""

    if route_count == 2:
        overrides = two_deployment_override()
    elif route_count == 3:
        overrides = three_deployment_override()
    else:
        overrides = six_deployment_override()
    model = cast(dict[str, Any], overrides["model"])
    endpoint_policies = cast(dict[str, dict[str, Any]], model["endpoint_policies"])
    deployments = cast(dict[str, dict[str, Any]], model["deployments"])
    for deployment in deployments.values():
        deployment["capabilities"] = ["text_completion", "text_stream"]
    for deployment_id in ["real_primary", "real_secondary"]:
        endpoint_name = f"{deployment_id}_endpoint"
        if deployment_id == "real_primary":
            endpoint_name = "real_primary_endpoint"
        endpoint_policies[endpoint_name]["completion_classifiers"] = [  # type: ignore[index]
            {"ref": "trusted_response_header_not_started", "version": "v1"}
        ]
        deployments[deployment_id]["completion_classifier_ref"] = (  # type: ignore[index]
            "trusted_response_header_not_started"
        )
        deployments[deployment_id]["completion_classifier_version"] = "v1"  # type: ignore[index]
    return load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def downstream_chain_policy(*, route_count: Literal[2, 3, 6] = 3) -> AgentModelPolicy:
    """用合法 A 投影和完整 route refs 构造 downstream 专用 policy。"""

    routes = ROUTES[:route_count]
    return AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
        fallback_routes=tuple(ModelRouteRef.model_validate(route) for route in routes),
    )
