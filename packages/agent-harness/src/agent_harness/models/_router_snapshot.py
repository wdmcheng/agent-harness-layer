"""从 durable budget-tree-v2 子快照恢复不可变模型路由。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from agent_harness.config.schemas import ModelSettings
from agent_harness.models._router_contracts import (
    FrozenAgentModelPolicy,
    FrozenModelRouteSnapshot,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouterConfig,
)
from agent_harness.models._router_snapshot_chain import RouterSnapshotChainPlanningMixin
from agent_harness.models.providers import ModelProvider, ModelRequest


class RouterSnapshotPlanningMixin(RouterSnapshotChainPlanningMixin):
    """只负责按冻结快照恢复 route；不读取当前 deployment 补齐旧身份。"""

    config: ModelRouterConfig
    _providers: dict[str, ModelProvider]
    _model_settings: ModelSettings | None

    @staticmethod
    def _minimum_limit(first: int | None, second: int | None) -> int | None:
        values = [value for value in (first, second) if value is not None]
        return min(values) if values else None

    def plan_from_snapshot(
        self,
        request: ModelRequest,
        *,
        snapshot: Mapping[str, Any],
        agent_id: str,
    ) -> ModelRoutePlan:
        """只从完整 v2 子快照重建真实 route；current settings 不参与路由选择。"""

        if snapshot.get("schema_version") != "budget-tree-v2":
            raise ModelRouteError("budget.reservation_rejected", "snapshot schema is invalid")
        raw_agents_value = snapshot.get("agents")
        if not isinstance(raw_agents_value, dict):
            raise ModelRouteError("budget.reservation_rejected", "snapshot agents are invalid")
        raw_agents = cast(dict[str, object], raw_agents_value)
        raw_target = raw_agents.get(agent_id)
        if not isinstance(raw_target, dict):
            raise ModelRouteError("budget.reservation_rejected", "snapshot target is invalid")
        target = cast(dict[str, object], raw_target)
        try:
            policy = FrozenAgentModelPolicy.model_validate(target.get("model_policy"))
        except Exception as exc:
            raise ModelRouteError(
                "budget.reservation_rejected", "snapshot model policy is invalid"
            ) from exc
        deployment_id = request.deployment_id or policy.deployment_id
        if deployment_id != policy.deployment_id:
            raise ModelRouteError("model.route_not_allowed", "request cannot change deployment")
        if request.provider is not None and request.provider != policy.provider:
            raise ModelRouteError("model.route_not_allowed", "provider assertion mismatch")
        if request.capability not in {"text_completion", "text_stream"}:
            raise ModelRouteError("model.capability_unsupported", "capability is not supported")
        selected_model = request.model or policy.default_model
        if selected_model not in policy.allowed_models:
            raise ModelRouteError("model.route_not_allowed", "model is outside frozen policy")
        raw_routes = target.get("routes")
        if not isinstance(raw_routes, list):
            raise ModelRouteError("budget.reservation_rejected", "snapshot routes are invalid")
        deployment_fallbacks = (
            policy.fallback_models
            if policy.deployment_fallback_models is None
            else policy.deployment_fallback_models
        )
        fallback_models = (
            []
            if request.model is not None
            else [
                model
                for model in policy.fallback_models
                if model in deployment_fallbacks
                and model in policy.allowed_models
                and model != selected_model
            ]
        )
        candidate_models = [selected_model, *fallback_models]
        candidate_plans: dict[str, ModelRoutePlan] = {}
        candidate_routes: dict[str, FrozenModelRouteSnapshot] = {}
        for candidate_model in candidate_models:
            matching: list[dict[str, object]] = []
            for raw_route in cast(list[object], raw_routes):
                if not isinstance(raw_route, dict):
                    continue
                item = cast(dict[str, object], raw_route)
                if (
                    item.get("usage_kind") == "model"
                    and item.get("deployment_id") == deployment_id
                    and item.get("provider") == policy.provider
                    and item.get("model") == candidate_model
                ):
                    matching.append(item)
            if len(matching) != 1:
                raise ModelRouteError("budget.reservation_rejected", "snapshot route is ambiguous")
            try:
                frozen = FrozenModelRouteSnapshot.model_validate(matching[0])
            except Exception as exc:
                raise ModelRouteError(
                    "budget.reservation_rejected", "snapshot route is incomplete"
                ) from exc
            candidate_request = request.model_copy(update={"model": candidate_model})
            candidate_routes[candidate_model] = frozen
            candidate_plans[candidate_model] = self._plan_frozen_route(
                candidate_request,
                policy=policy,
                frozen=frozen,
            )

        hard_token_limit, hard_cost_limit = self._snapshot_target_budget(target)
        preferred = candidate_plans[selected_model]
        preferred_frozen = candidate_routes[selected_model]
        preferred_hard = self._over_hard_budget(
            preferred,
            token_limit=hard_token_limit,
            cost_limit=hard_cost_limit,
        )
        preferred_soft = self._over_soft_budget(
            preferred,
            token_limit=preferred_frozen.soft_max_tokens_per_call,
            cost_limit=None,
        )
        if not preferred_hard and not preferred_soft:
            return self._with_route_decision(
                preferred,
                action="call",
                token_limit=self._minimum_limit(
                    preferred_frozen.soft_max_tokens_per_call,
                    hard_token_limit,
                ),
                cost_limit=hard_cost_limit,
            )
        for fallback_model in fallback_models:
            candidate = candidate_plans[fallback_model]
            frozen = candidate_routes[fallback_model]
            if self._over_hard_budget(
                candidate,
                token_limit=hard_token_limit,
                cost_limit=hard_cost_limit,
            ) or self._over_soft_budget(
                candidate,
                token_limit=frozen.soft_max_tokens_per_call,
                cost_limit=None,
            ):
                continue
            return self._with_route_decision(
                candidate,
                action="fallback",
                fallback_model=fallback_model,
                reason="preferred route exceeds configured budget threshold",
                token_limit=self._minimum_limit(
                    frozen.soft_max_tokens_per_call,
                    hard_token_limit,
                ),
                cost_limit=hard_cost_limit,
            )
        return self._with_route_decision(
            preferred,
            action="policy_required",
            reason="estimated budget exceeds threshold and no fallback is eligible",
            approval_kind=None if preferred_hard else "soft_budget",
            token_limit=self._minimum_limit(
                preferred_frozen.soft_max_tokens_per_call,
                hard_token_limit,
            ),
            cost_limit=hard_cost_limit,
        )
