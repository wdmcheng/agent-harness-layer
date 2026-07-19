"""0016 frozen agent budget 与 route catalog 校验。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import cast

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.values import _decimal


def _snapshot_catalog_valid(
    snapshot: Mapping[str, object],
    *,
    tree_agents: set[str],
    token_limit: int,
    cost_enabled: bool,
    cost_limit: Decimal | None,
) -> bool:
    """验证 bundle 自带的 source/target descriptor、budget、route 与 price catalog。"""

    raw_agents = snapshot.get("agents")
    if not isinstance(raw_agents, Mapping):
        return False
    agents = cast(Mapping[object, object], raw_agents)
    for agent_id in tree_agents:
        raw_sub_snapshot = agents.get(agent_id)
        if not isinstance(raw_sub_snapshot, Mapping):
            return False
        sub_snapshot = cast(Mapping[str, object], raw_sub_snapshot)
        policy = sub_snapshot.get("model_policy")
        target_budget = sub_snapshot.get("target_budget")
        routes = sub_snapshot.get("routes")
        if (
            sub_snapshot.get("agent_id") != agent_id
            or not isinstance(sub_snapshot.get("descriptor_version"), str)
            or not sub_snapshot.get("descriptor_version")
            or not isinstance(policy, Mapping)
            or not isinstance(target_budget, Mapping)
            or not isinstance(routes, list)
            or not routes
        ):
            return False
        typed_policy = cast(Mapping[str, object], policy)
        raw_fallbacks = typed_policy.get("fallback_models")
        if (
            not isinstance(typed_policy.get("provider"), str)
            or not typed_policy.get("provider")
            or not isinstance(typed_policy.get("default_model"), str)
            or not typed_policy.get("default_model")
            or not isinstance(raw_fallbacks, list)
        ):
            return False
        provider = cast(str, typed_policy["provider"])
        default_model = cast(str, typed_policy["default_model"])
        fallback_values = cast(list[object], raw_fallbacks)
        if not all(isinstance(item, str) and item for item in fallback_values):
            return False
        policy_models = [default_model, *cast(list[str], fallback_values)]
        if len(set(policy_models)) != len(policy_models):
            return False
        allowed_model_routes = {(provider, model) for model in policy_models}
        typed_budget = cast(Mapping[str, object], target_budget)
        target_tokens = typed_budget.get("max_tokens_per_run")
        if (
            isinstance(target_tokens, bool)
            or not isinstance(target_tokens, int)
            or target_tokens < 0
            or target_tokens > token_limit
        ):
            return False
        raw_target_cost = typed_budget.get("max_cost_usd_per_run")
        try:
            target_cost = (
                None
                if raw_target_cost is None
                else _decimal(raw_target_cost, field="target cost limit")
            )
        except RuntimeError:
            return False
        if (not cost_enabled and target_cost is not None) or (
            cost_enabled and target_cost is not None and target_cost > cast(Decimal, cost_limit)
        ):
            return False
        frozen_model_routes: list[tuple[str, str]] = []
        for raw_route in cast(list[object], routes):
            if not isinstance(raw_route, Mapping):
                return False
            route = cast(Mapping[str, object], raw_route)
            usage_kind = route.get("usage_kind")
            if usage_kind not in {"model", "embedding"} or any(
                not isinstance(route.get(field), str) or not route.get(field)
                for field in (
                    "provider",
                    "model",
                    "price_source_ref",
                    "price_source_version",
                )
            ):
                return False
            if "input_token_price_usd" not in route or (
                usage_kind == "model" and "output_token_price_usd" not in route
            ):
                return False
            input_price = route.get("input_token_price_usd")
            output_price = route.get("output_token_price_usd")
            if cost_enabled and (
                input_price is None or (usage_kind == "model" and output_price is None)
            ):
                return False
            try:
                if input_price is not None:
                    _decimal(input_price, field="input token price")
                if usage_kind == "model" and output_price is not None:
                    _decimal(output_price, field="output token price")
            except RuntimeError:
                return False
            if usage_kind == "model":
                route_key = (cast(str, route["provider"]), cast(str, route["model"]))
                if route_key not in allowed_model_routes:
                    return False
                frozen_model_routes.append(route_key)
        if (
            len(frozen_model_routes) != len(allowed_model_routes)
            or set(frozen_model_routes) != allowed_model_routes
        ):
            return False
    return True


__all__ = ["_snapshot_catalog_valid"]
