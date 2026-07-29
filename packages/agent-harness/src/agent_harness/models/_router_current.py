"""当前 typed deployment 与 Agent policy 的受控路由规划。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal, cast

from agent_harness.config.model_endpoints import (
    ResolvedModelDeployment,
    resolve_model_deployment,
)
from agent_harness.config.schemas import ModelSettings
from agent_harness.models._router_contracts import (
    AgentModelPolicyLike,
    ModelBulkheadPolicy,
    ModelRetryPolicy,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouterConfig,
)
from agent_harness.models.providers import ModelDecision, ModelProvider, ModelRequest


class RouterCurrentPlanningMixin:
    """只负责当前配置、fallback、预算界限和 legacy fake 的纯规划。"""

    config: ModelRouterConfig
    _providers: dict[str, ModelProvider]
    _model_settings: ModelSettings | None

    def _plan_controlled(
        self,
        request: ModelRequest,
        *,
        agent_policy: AgentModelPolicyLike,
    ) -> ModelRoutePlan:
        model_settings = self._model_settings
        assert model_settings is not None
        agent_deployment = str(agent_policy.deployment_id)
        deployment_id = request.deployment_id or agent_deployment
        if deployment_id != agent_deployment:
            raise ModelRouteError("model.route_not_allowed", "request cannot change deployment")
        try:
            resolved = resolve_model_deployment(model_settings, deployment_id)
        except ValueError as exc:
            raise ModelRouteError("model.route_not_allowed", "deployment is not available") from exc
        agent_provider = str(agent_policy.provider)
        if agent_provider != resolved.provider_kind or (
            request.provider is not None and request.provider != resolved.provider_kind
        ):
            raise ModelRouteError("model.route_not_allowed", "provider assertion mismatch")
        provider = self._providers.get(resolved.provider_kind)
        if provider is None or provider.provider_id != resolved.provider_kind:
            raise ModelRouteError("model.route_not_allowed", "bound provider identity mismatch")
        if request.capability != "text_completion":
            raise ModelRouteError("model.capability_unsupported", "capability is not supported")
        deployment = model_settings.deployments[deployment_id]
        if request.capability not in deployment.capabilities:
            raise ModelRouteError("model.capability_unsupported", "deployment capability mismatch")
        agent_allowed = set(agent_policy.allowed_models)
        allowed = tuple(model for model in resolved.allowed_models if model in agent_allowed)
        if not allowed:
            raise ModelRouteError(
                "model.route_not_allowed", "deployment and agent model sets do not intersect"
            )
        selected_model = request.model or str(agent_policy.default_model)
        if selected_model not in allowed:
            raise ModelRouteError("model.route_not_allowed", "model is outside frozen intersection")
        if (
            request.max_output_tokens < 1
            or request.max_output_tokens > deployment.max_output_tokens
        ):
            raise ModelRouteError("model.route_not_allowed", "output cap cannot exceed deployment")
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if prompt_bytes > deployment.max_prompt_utf8_bytes:
            raise ModelRouteError("model.route_not_allowed", "prompt exceeds deployment byte cap")
        fallback_models = (
            []
            if request.model is not None
            else [
                model
                for model in agent_policy.fallback_models
                if model in resolved.fallback_models
                and model in allowed
                and model != selected_model
            ]
        )
        candidate_models = [selected_model, *fallback_models]
        candidate_plans = {
            model: self._build_current_route_plan(
                request.model_copy(update={"model": model}),
                deployment_id=deployment_id,
                allowed=allowed,
                resolved=resolved,
            )
            for model in candidate_models
        }
        preferred = candidate_plans[selected_model]
        preferred_token_limit = self.config.route_max_tokens_per_call.get(
            selected_model, self.config.max_tokens_per_call
        )
        preferred_cost_limit = self.config.route_max_cost_per_call.get(
            selected_model, self.config.max_cost_per_call
        )
        preferred_over = self._over_soft_budget(
            preferred,
            token_limit=preferred_token_limit,
            cost_limit=preferred_cost_limit,
        )
        if not preferred_over:
            return self._with_route_decision(
                preferred,
                action="call",
                token_limit=preferred_token_limit,
                cost_limit=preferred_cost_limit,
            )
        for fallback_model in fallback_models:
            candidate = candidate_plans[fallback_model]
            token_limit = self.config.route_max_tokens_per_call.get(
                fallback_model, self.config.max_tokens_per_call
            )
            cost_limit = self.config.route_max_cost_per_call.get(
                fallback_model, self.config.max_cost_per_call
            )
            if self._over_soft_budget(
                candidate,
                token_limit=token_limit,
                cost_limit=cost_limit,
            ):
                continue
            return self._with_route_decision(
                candidate,
                action="fallback",
                fallback_model=fallback_model,
                reason="preferred route exceeds configured budget threshold",
                token_limit=token_limit,
                cost_limit=cost_limit,
            )
        return self._with_route_decision(
            preferred,
            action="policy_required",
            reason="estimated budget exceeds threshold and no fallback is eligible",
            approval_kind="soft_budget",
            token_limit=preferred_token_limit,
            cost_limit=preferred_cost_limit,
        )

    def _build_current_route_plan(
        self,
        request: ModelRequest,
        *,
        deployment_id: str,
        allowed: tuple[str, ...],
        resolved: ResolvedModelDeployment,
    ) -> ModelRoutePlan:
        """用单个候选的受信目录重算 immutable plan，不读取 request 外的 override。"""

        model_settings = self._model_settings
        assert model_settings is not None
        deployment = model_settings.deployments[deployment_id]
        selected_model = request.model
        assert selected_model is not None
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if resolved.provider_kind == "fake":
            per_attempt_tokens = prompt_bytes + request.max_output_tokens
            if per_attempt_tokens > resolved.max_per_attempt_token_bound:
                raise ModelRouteError(
                    "budget.reservation_rejected",
                    "dynamic token bound exceeds static ceiling",
                )
            return ModelRoutePlan(
                deployment_id=deployment_id,
                provider_kind="fake",
                provider="fake",
                allowed_models=allowed,
                model=selected_model,
                capability=request.capability,
                decision=ModelDecision(action="call", estimated_tokens=per_attempt_tokens),
                prompt_utf8_bytes=prompt_bytes,
                trusted_input_token_bound=prompt_bytes,
                output_token_cap=request.max_output_tokens,
                per_attempt_token_bound=per_attempt_tokens,
                max_attempts=1,
                reserved_token_bound=per_attempt_tokens,
                connect_timeout_ms=deployment.connect_timeout_ms,
                read_timeout_ms=deployment.read_timeout_ms,
                total_timeout_ms=deployment.total_timeout_ms,
                bulkhead_policy=ModelBulkheadPolicy(
                    max_in_flight=deployment.max_in_flight,
                    queue_timeout_ms=deployment.queue_timeout_ms,
                ),
                snapshot_schema_version="budget-tree-v1",
                trusted_token_bound=per_attempt_tokens,
                trusted_cost_bound=None,
            )

        catalog = resolved.model_catalogs[selected_model]
        static_token_ceiling = (
            deployment.max_prompt_utf8_bytes
            + catalog.input_envelope_token_bound
            + deployment.max_output_tokens
        )
        if static_token_ceiling > resolved.max_per_attempt_token_bound:
            raise ModelRouteError(
                "budget.reservation_rejected", "candidate token formula exceeds deployment"
            )
        trusted_input = prompt_bytes + catalog.input_envelope_token_bound
        per_attempt_tokens = trusted_input + request.max_output_tokens
        if per_attempt_tokens > static_token_ceiling:
            raise ModelRouteError(
                "budget.reservation_rejected", "dynamic token bound exceeds static ceiling"
            )
        per_attempt_cost: Decimal | None = None
        reserved_cost: Decimal | None = None
        if catalog.cost_enabled:
            if (
                catalog.input_token_price_usd is None
                or catalog.output_token_price_usd is None
                or catalog.price_source_ref is None
                or catalog.price_source_version is None
                or resolved.max_per_attempt_cost_bound is None
            ):
                raise ModelRouteError(
                    "budget.reservation_rejected", "candidate price identity is incomplete"
                )
            static_cost_ceiling = (
                Decimal(deployment.max_prompt_utf8_bytes + catalog.input_envelope_token_bound)
                * catalog.input_token_price_usd
                + Decimal(deployment.max_output_tokens) * catalog.output_token_price_usd
            )
            if static_cost_ceiling > resolved.max_per_attempt_cost_bound:
                raise ModelRouteError(
                    "budget.reservation_rejected", "candidate cost formula exceeds deployment"
                )
            per_attempt_cost = (
                Decimal(trusted_input) * catalog.input_token_price_usd
                + Decimal(request.max_output_tokens) * catalog.output_token_price_usd
            )
            if per_attempt_cost > static_cost_ceiling:
                raise ModelRouteError(
                    "budget.reservation_rejected", "dynamic cost bound exceeds static ceiling"
                )
            reserved_cost = per_attempt_cost * deployment.max_attempts
        return ModelRoutePlan(
            deployment_id=deployment_id,
            provider_kind=resolved.provider_kind,
            provider=resolved.provider_kind,
            allowed_models=allowed,
            model=selected_model,
            capability=request.capability,
            decision=ModelDecision(
                action="call",
                estimated_tokens=per_attempt_tokens,
                estimated_cost_usd=per_attempt_cost,
                price_source_ref=catalog.price_source_ref,
                price_source_version=catalog.price_source_version,
            ),
            canonical_base_url=resolved.canonical_base_url,
            endpoint_origin=resolved.endpoint_origin,
            endpoint_policy_ref=resolved.endpoint_policy_ref,
            endpoint_policy_version=resolved.endpoint_policy_version,
            endpoint_policy_digest=resolved.endpoint_policy_digest,
            completion_classifier_ref=deployment.completion_classifier_ref,
            completion_classifier_version=deployment.completion_classifier_version,
            credential_ref=resolved.credential_ref,
            model_catalog_ref=deployment.model_catalog_refs[selected_model],
            model_catalog_version=catalog.version,
            model_catalog_digest=catalog.digest,
            request_shape_ref=catalog.request_shape_ref,
            request_shape_version=catalog.request_shape_version,
            input_bound_strategy_ref=catalog.input_bound_strategy_ref,
            input_bound_strategy_version=catalog.input_bound_strategy_version,
            input_envelope_token_bound=catalog.input_envelope_token_bound,
            prompt_utf8_bytes=prompt_bytes,
            trusted_input_token_bound=trusted_input,
            output_token_cap=request.max_output_tokens,
            per_attempt_token_bound=per_attempt_tokens,
            per_attempt_cost_bound=per_attempt_cost,
            max_attempts=deployment.max_attempts,
            reserved_token_bound=per_attempt_tokens * deployment.max_attempts,
            reserved_cost_bound=reserved_cost,
            input_token_price_usd=catalog.input_token_price_usd,
            output_token_price_usd=catalog.output_token_price_usd,
            price_source_ref=catalog.price_source_ref,
            price_source_version=catalog.price_source_version,
            connect_timeout_ms=deployment.connect_timeout_ms,
            read_timeout_ms=deployment.read_timeout_ms,
            total_timeout_ms=deployment.total_timeout_ms,
            retry_policy=ModelRetryPolicy(
                retryable_http_statuses=tuple(deployment.retryable_http_statuses),
                max_attempts=deployment.max_attempts,
                max_wait_ms=deployment.max_retry_wait_ms,
                backoff_initial_ms=deployment.backoff_initial_ms,
                backoff_max_ms=deployment.backoff_max_ms,
            ),
            bulkhead_policy=ModelBulkheadPolicy(
                max_in_flight=deployment.max_in_flight,
                queue_timeout_ms=deployment.queue_timeout_ms,
            ),
            snapshot_schema_version="budget-tree-v2",
            trusted_token_bound=per_attempt_tokens * deployment.max_attempts,
            trusted_cost_bound=reserved_cost,
        )

    @staticmethod
    def _snapshot_target_budget(target: Mapping[str, object]) -> tuple[int, Decimal | None]:
        """解析冻结 target hard limit；缺失或非有限值一律视为损坏快照。"""

        raw_budget = target.get("target_budget")
        if not isinstance(raw_budget, dict):
            raise ModelRouteError("budget.reservation_rejected", "snapshot budget is invalid")
        budget = cast(dict[str, object], raw_budget)
        token_limit = budget.get("max_tokens_per_run")
        if isinstance(token_limit, bool) or not isinstance(token_limit, int) or token_limit < 0:
            raise ModelRouteError("budget.reservation_rejected", "snapshot token budget is invalid")
        raw_cost = budget.get("max_cost_usd_per_run")
        if raw_cost is None:
            return token_limit, None
        try:
            cost_limit = Decimal(str(raw_cost))
        except Exception as exc:
            raise ModelRouteError(
                "budget.reservation_rejected", "snapshot cost budget is invalid"
            ) from exc
        if not cost_limit.is_finite() or cost_limit < 0:
            raise ModelRouteError("budget.reservation_rejected", "snapshot cost budget is invalid")
        return token_limit, cost_limit

    @staticmethod
    def _over_soft_budget(
        plan: ModelRoutePlan,
        *,
        token_limit: int | None,
        cost_limit: Decimal | None,
    ) -> bool:
        """比较调用级 soft threshold；启用 cost 却无可信价格时 fail closed。"""

        if token_limit is not None and plan.per_attempt_token_bound > token_limit:
            return True
        if cost_limit is None:
            return False
        if plan.per_attempt_cost_bound is None:
            raise ModelRouteError(
                "budget.reservation_rejected", "cost threshold requires trusted price"
            )
        return plan.per_attempt_cost_bound > cost_limit

    @staticmethod
    def _over_hard_budget(
        plan: ModelRoutePlan,
        *,
        token_limit: int,
        cost_limit: Decimal | None,
    ) -> bool:
        """按完整 attempt reservation 比较冻结 target hard limit。"""

        if plan.reserved_token_bound > token_limit:
            return True
        if cost_limit is None:
            return False
        if plan.reserved_cost_bound is None:
            raise ModelRouteError(
                "budget.reservation_rejected", "hard cost budget requires trusted price"
            )
        return plan.reserved_cost_bound > cost_limit

    @staticmethod
    def _minimum_limit(first: int | None, second: int | None) -> int | None:
        """返回两个可选 token limit 中更严格者。"""

        limits = [value for value in (first, second) if value is not None]
        return min(limits) if limits else None

    @staticmethod
    def _with_route_decision(
        plan: ModelRoutePlan,
        *,
        action: str,
        token_limit: int | None,
        cost_limit: Decimal | None,
        fallback_model: str | None = None,
        reason: str | None = None,
        approval_kind: Literal["soft_budget"] | None = None,
    ) -> ModelRoutePlan:
        """只替换可审计 decision；冻结 route identity 与上界保持同一候选。"""

        decision = ModelDecision(
            action=action,
            estimated_tokens=plan.per_attempt_token_bound,
            max_tokens=token_limit,
            estimated_cost_usd=plan.per_attempt_cost_bound,
            max_cost_usd=cost_limit,
            fallback_model=fallback_model,
            reason=reason,
            price_source_ref=plan.price_source_ref,
            price_source_version=plan.price_source_version,
        )
        return plan.model_copy(update={"decision": decision, "approval_kind": approval_kind})

    def _plan_legacy_fake(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None,
    ) -> ModelRoutePlan:
        active = config or self.config
        provider_id = request.provider or active.default_provider
        if provider_id not in self._providers:
            raise KeyError(f"model provider is not configured: {provider_id}")
        trusted_input_bound = len(request.prompt.encode("utf-8"))
        estimated_tokens = trusted_input_bound + request.max_output_tokens
        selected_model = request.model or active.default_model
        action = "call"
        fallback_model = None
        reason = None
        if active.max_tokens_per_call is not None and estimated_tokens > active.max_tokens_per_call:
            fallback_model = next(
                (
                    model
                    for model in active.fallback_models
                    if estimated_tokens
                    <= active.route_max_tokens_per_call.get(model, active.max_tokens_per_call)
                ),
                None,
            )
            if fallback_model is not None:
                action = "fallback"
                selected_model = fallback_model
                reason = "estimated tokens exceed budget"
            else:
                action = "policy_required"
                reason = "estimated tokens exceed budget and no fallback is configured"
        decision = ModelDecision(
            action=action,
            estimated_tokens=estimated_tokens,
            max_tokens=active.max_tokens_per_call,
            fallback_model=fallback_model,
            reason=reason,
            price_source_ref=active.route_price_source_refs.get(
                selected_model, active.price_source_ref
            ),
            price_source_version=active.route_price_source_versions.get(
                selected_model, active.price_source_version
            ),
        )
        input_price = active.route_input_token_prices_usd.get(
            selected_model, active.input_token_price_usd
        )
        output_price = active.route_output_token_prices_usd.get(
            selected_model, active.output_token_price_usd
        )
        cost = None
        if input_price is not None and output_price is not None:
            cost = (
                Decimal(trusted_input_bound) * input_price
                + Decimal(request.max_output_tokens) * output_price
            )
        return ModelRoutePlan(
            provider=provider_id,
            provider_kind=provider_id,
            allowed_models=(selected_model,),
            model=selected_model,
            decision=decision,
            approval_kind="soft_budget" if action == "policy_required" else None,
            prompt_utf8_bytes=trusted_input_bound,
            trusted_input_token_bound=trusted_input_bound,
            output_token_cap=request.max_output_tokens,
            per_attempt_token_bound=estimated_tokens,
            per_attempt_cost_bound=cost,
            reserved_token_bound=estimated_tokens,
            reserved_cost_bound=cost,
            trusted_token_bound=estimated_tokens,
            trusted_cost_bound=cost,
            input_token_price_usd=input_price,
            output_token_price_usd=output_price,
            price_source_ref=decision.price_source_ref,
            price_source_version=decision.price_source_version,
            connect_timeout_ms=active.timeout_seconds * 1000,
            read_timeout_ms=active.timeout_seconds * 1000,
            total_timeout_ms=active.timeout_seconds * 1000,
        )
