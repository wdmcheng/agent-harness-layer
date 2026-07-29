"""Shared-budget 冻结快照恢复投影私有 seam。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from agent_harness.models.router import ModelRouterConfig
from agent_harness.runtime._shared_budget_common import price_from_snapshot


def model_router_config(
    *,
    snapshot: dict[str, Any],
    agent_id: str,
    base: ModelRouterConfig,
) -> ModelRouterConfig:
    """只从 frozen target sub-snapshot 投影当前 agent 的实际 model route。"""

    raw_agents: object = snapshot.get("agents")
    if not isinstance(raw_agents, dict):
        raise ValueError("shared budget target model snapshot is invalid")
    agents = cast(dict[str, object], raw_agents)
    raw_target = agents.get(agent_id)
    if not isinstance(raw_target, dict):
        raise ValueError("shared budget target model snapshot is invalid")
    target = cast(dict[str, object], raw_target)
    raw_policy = target.get("model_policy")
    raw_routes = target.get("routes")
    if not isinstance(raw_policy, dict) or not isinstance(raw_routes, list):
        raise ValueError("shared budget target model snapshot is invalid")
    policy = cast(dict[str, object], raw_policy)
    routes = cast(list[object], raw_routes)
    provider = policy.get("provider")
    default_model = policy.get("default_model")
    raw_fallback_models = policy.get("fallback_models")
    fallback_values = (
        cast(list[object], raw_fallback_models) if isinstance(raw_fallback_models, list) else []
    )
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(default_model, str)
        or not default_model
        or not isinstance(raw_fallback_models, list)
        or any(not isinstance(item, str) or not item for item in fallback_values)
    ):
        raise ValueError("shared budget target model policy is invalid")
    fallback_models = [cast(str, item) for item in fallback_values]
    route_refs: dict[str, str] = {}
    route_versions: dict[str, str] = {}
    route_input_prices: dict[str, Decimal] = {}
    route_output_prices: dict[str, Decimal] = {}
    route_limits: dict[str, int] = {}
    for raw_route in routes:
        if not isinstance(raw_route, dict):
            continue
        raw = cast(dict[str, object], raw_route)
        if raw.get("usage_kind") != "model" or raw.get("provider") != provider:
            continue
        model = raw.get("model")
        ref = raw.get("price_source_ref")
        version = raw.get("price_source_version")
        if not isinstance(model, str) or not isinstance(ref, str) or not isinstance(version, str):
            continue
        route_refs[model] = ref
        route_versions[model] = version
        input_price = price_from_snapshot(raw.get("input_token_price_usd"))
        output_price = price_from_snapshot(raw.get("output_token_price_usd"))
        if input_price is not None:
            route_input_prices[model] = input_price
        if output_price is not None:
            route_output_prices[model] = output_price
        soft_limit = raw.get("soft_max_tokens_per_call")
        if isinstance(soft_limit, int) and not isinstance(soft_limit, bool) and soft_limit >= 0:
            route_limits[model] = soft_limit
    allowed = {default_model, *fallback_models}
    if not allowed <= set(route_refs):
        raise ValueError("shared budget target route catalog is incomplete")
    raw_owner = snapshot.get("owner")
    if not isinstance(raw_owner, dict):
        raise ValueError("shared budget owner snapshot is invalid")
    cost_enabled = cast(dict[str, object], raw_owner).get("cost_enabled")
    if cost_enabled is True and (
        not allowed <= set(route_input_prices) or not allowed <= set(route_output_prices)
    ):
        raise ValueError("shared budget target route price is incomplete")
    default_limit = route_limits.get(default_model)
    return base.model_copy(
        update={
            "default_provider": provider,
            "default_model": default_model,
            "fallback_models": list(fallback_models),
            "max_tokens_per_call": default_limit,
            "input_token_price_usd": route_input_prices.get(default_model),
            "output_token_price_usd": route_output_prices.get(default_model),
            "price_source_ref": route_refs[default_model],
            "price_source_version": route_versions[default_model],
            "route_price_source_refs": route_refs,
            "route_price_source_versions": route_versions,
            "route_input_token_prices_usd": route_input_prices,
            "route_output_token_prices_usd": route_output_prices,
            "route_max_tokens_per_call": route_limits,
        }
    )


def embedding_price_config(
    *,
    snapshot: dict[str, Any],
    agent_id: str,
    provider: str,
    model: str,
) -> tuple[Decimal | None, str, str]:
    """从 target sub-snapshot 解析 embedding 的冻结价格。"""

    raw_agents = snapshot.get("agents")
    raw_owner = snapshot.get("owner")
    if not isinstance(raw_agents, dict) or not isinstance(raw_owner, dict):
        raise ValueError("shared budget embedding snapshot is invalid")
    raw_target = cast(dict[str, object], raw_agents).get(agent_id)
    if not isinstance(raw_target, dict):
        raise ValueError("shared budget embedding snapshot is invalid")
    raw_routes = cast(dict[str, object], raw_target).get("routes")
    if not isinstance(raw_routes, list):
        raise ValueError("shared budget embedding snapshot is invalid")
    for raw_route in cast(list[object], raw_routes):
        if not isinstance(raw_route, dict):
            continue
        route = cast(dict[str, object], raw_route)
        if (
            route.get("usage_kind") != "embedding"
            or route.get("provider") != provider
            or route.get("model") != model
        ):
            continue
        ref = route.get("price_source_ref")
        version = route.get("price_source_version")
        if not isinstance(ref, str) or not ref or not isinstance(version, str) or not version:
            break
        price = price_from_snapshot(route.get("input_token_price_usd"))
        return price, ref, version
    raise ValueError("shared budget embedding route price is incomplete")


__all__ = ["embedding_price_config", "model_router_config"]
