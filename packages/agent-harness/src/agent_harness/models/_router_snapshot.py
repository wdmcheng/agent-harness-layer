"""从 durable budget-tree-v2 子快照恢复不可变模型路由。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

from agent_harness.config.schemas import ModelSettings
from agent_harness.models._router_contracts import (
    AgentModelPolicyLike,
    FrozenAgentModelPolicy,
    FrozenModelRouteSnapshot,
    ModelBulkheadPolicy,
    ModelRetryPolicy,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouterConfig,
)
from agent_harness.models._router_current import RouterCurrentPlanningMixin
from agent_harness.models.providers import ModelDecision, ModelProvider, ModelRequest


class RouterSnapshotPlanningMixin(RouterCurrentPlanningMixin):
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

    def _plan_frozen_route(
        self,
        request: ModelRequest,
        *,
        policy: AgentModelPolicyLike,
        frozen: FrozenModelRouteSnapshot,
    ) -> ModelRoutePlan:
        """对快照静态输入重做动态 hard eligibility 与 checked reservation 公式。"""

        provider = self._providers.get(frozen.provider)
        if provider is None or provider.provider_id != frozen.provider:
            raise ModelRouteError("model.route_not_allowed", "bound provider identity mismatch")
        if request.capability not in frozen.capabilities:
            raise ModelRouteError("model.capability_unsupported", "snapshot capability mismatch")
        if request.max_output_tokens < 1 or request.max_output_tokens > frozen.max_output_tokens:
            raise ModelRouteError("model.route_not_allowed", "output cap cannot exceed snapshot")
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if prompt_bytes > frozen.max_prompt_utf8_bytes:
            raise ModelRouteError("model.route_not_allowed", "prompt exceeds snapshot byte cap")
        expected_static_tokens = (
            frozen.max_prompt_utf8_bytes
            + frozen.input_envelope_token_bound
            + frozen.max_output_tokens
        )
        if expected_static_tokens != frozen.max_per_attempt_token_bound:
            raise ModelRouteError("budget.reservation_rejected", "snapshot token formula mismatch")
        trusted_input = prompt_bytes + frozen.input_envelope_token_bound
        per_attempt_tokens = trusted_input + request.max_output_tokens
        if per_attempt_tokens > frozen.max_per_attempt_token_bound:
            raise ModelRouteError(
                "budget.reservation_rejected", "dynamic token bound exceeds snapshot"
            )
        per_attempt_cost: Decimal | None = None
        reserved_cost: Decimal | None = None
        if frozen.cost_enabled:
            if (
                frozen.input_token_price_usd is None
                or frozen.output_token_price_usd is None
                or frozen.price_source_ref is None
                or frozen.price_source_version is None
            ):
                raise ModelRouteError("budget.reservation_rejected", "snapshot price is incomplete")
            expected_static_cost = (
                Decimal(frozen.max_prompt_utf8_bytes + frozen.input_envelope_token_bound)
                * frozen.input_token_price_usd
                + Decimal(frozen.max_output_tokens) * frozen.output_token_price_usd
            )
            if expected_static_cost != frozen.max_per_attempt_cost_bound:
                raise ModelRouteError(
                    "budget.reservation_rejected", "snapshot cost formula mismatch"
                )
            per_attempt_cost = (
                Decimal(trusted_input) * frozen.input_token_price_usd
                + Decimal(request.max_output_tokens) * frozen.output_token_price_usd
            )
            reserved_cost = per_attempt_cost * frozen.max_attempts
        elif any(
            item is not None
            for item in (
                frozen.input_token_price_usd,
                frozen.output_token_price_usd,
                frozen.price_source_ref,
                frozen.price_source_version,
                frozen.max_per_attempt_cost_bound,
            )
        ):
            raise ModelRouteError(
                "budget.reservation_rejected", "cost-disabled snapshot is invalid"
            )
        decision = ModelDecision(
            action="call",
            estimated_tokens=per_attempt_tokens,
            max_tokens=frozen.max_per_attempt_token_bound,
            price_source_ref=frozen.price_source_ref,
            price_source_version=frozen.price_source_version,
        )
        return ModelRoutePlan(
            deployment_id=frozen.deployment_id,
            provider_kind=frozen.provider,
            provider=frozen.provider,
            allowed_models=tuple(policy.allowed_models),
            model=frozen.model,
            capability=request.capability,
            decision=decision,
            canonical_base_url=frozen.canonical_base_url,
            endpoint_origin=frozen.endpoint_origin,
            endpoint_policy_ref=frozen.endpoint_policy_ref,
            endpoint_policy_version=frozen.endpoint_policy_version,
            endpoint_policy_digest=frozen.endpoint_policy_digest,
            completion_classifier_ref=frozen.completion_classifier_ref,
            completion_classifier_version=frozen.completion_classifier_version,
            credential_ref=frozen.credential_ref,
            model_catalog_ref=frozen.model_catalog_ref,
            model_catalog_version=frozen.model_catalog_version,
            model_catalog_digest=frozen.model_catalog_digest,
            request_shape_ref=frozen.request_shape_ref,
            request_shape_version=frozen.request_shape_version,
            input_bound_strategy_ref=frozen.input_bound_strategy_ref,
            input_bound_strategy_version=frozen.input_bound_strategy_version,
            input_envelope_token_bound=frozen.input_envelope_token_bound,
            prompt_utf8_bytes=prompt_bytes,
            trusted_input_token_bound=trusted_input,
            output_token_cap=request.max_output_tokens,
            per_attempt_token_bound=per_attempt_tokens,
            per_attempt_cost_bound=per_attempt_cost,
            max_attempts=frozen.max_attempts,
            reserved_token_bound=per_attempt_tokens * frozen.max_attempts,
            reserved_cost_bound=reserved_cost,
            input_token_price_usd=frozen.input_token_price_usd,
            output_token_price_usd=frozen.output_token_price_usd,
            price_source_ref=frozen.price_source_ref,
            price_source_version=frozen.price_source_version,
            connect_timeout_ms=frozen.connect_timeout_ms,
            read_timeout_ms=frozen.read_timeout_ms,
            total_timeout_ms=frozen.total_timeout_ms,
            retry_policy=ModelRetryPolicy.model_validate(frozen.retry_policy),
            bulkhead_policy=ModelBulkheadPolicy.model_validate(frozen.bulkhead_policy),
            snapshot_schema_version="budget-tree-v2",
            trusted_token_bound=per_attempt_tokens * frozen.max_attempts,
            trusted_cost_bound=reserved_cost,
        )
