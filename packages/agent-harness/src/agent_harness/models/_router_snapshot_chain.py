"""从 _router_snapshot.py 拆出的私有职责模块；公共 façade 与顺序语义保持不变。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal, cast

from agent_harness.config.schemas import ModelRouteRef, ModelSettings
from agent_harness.models._router_contracts import (
    AgentModelPolicyLike,
    FrozenAgentModelPolicy,
    FrozenModelRouteSnapshot,
    ModelBulkheadPolicy,
    ModelRetryPolicy,
    ModelRouteAgentPolicyIdentity,
    ModelRouteCandidate,
    ModelRouteChainPlan,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouterConfig,
    ModelRouteRequestBounds,
)
from agent_harness.models._router_current import RouterCurrentPlanningMixin
from agent_harness.models._router_identity import (
    model_route_digest,
    route_plan_identity_payload,
)
from agent_harness.models.providers import ModelDecision, ModelProvider, ModelRequest
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolIntentRequestIdentity,
    provider_tool_catalog_bytes,
)


class RouterSnapshotChainPlanningMixin(RouterCurrentPlanningMixin):
    """承载从兼容入口拆出的单一私有职责。"""

    config: ModelRouterConfig
    _providers: dict[str, ModelProvider]
    _model_settings: ModelSettings | None

    def plan_chain_from_snapshot(
        self,
        request: ModelRequest,
        *,
        snapshot: Mapping[str, Any],
        agent_id: str,
    ) -> ModelRouteChainPlan:
        """只从 durable v2 子快照恢复完整 chain，不读取 current settings 补值。"""

        if snapshot.get("schema_version") != "budget-tree-v2":
            raise ModelRouteError("budget.reservation_rejected", "snapshot schema is invalid")
        raw_agents = snapshot.get("agents")
        if not isinstance(raw_agents, dict):
            raise ModelRouteError("budget.reservation_rejected", "snapshot agents are invalid")
        raw_target = cast(dict[str, object], raw_agents).get(agent_id)
        if not isinstance(raw_target, dict):
            raise ModelRouteError("budget.reservation_rejected", "snapshot target is invalid")
        target = cast(dict[str, object], raw_target)
        try:
            policy = FrozenAgentModelPolicy.model_validate(target.get("model_policy"))
        except Exception as exc:
            raise ModelRouteError(
                "budget.reservation_rejected", "snapshot model policy is invalid"
            ) from exc
        authorized = list(policy.fallback_routes)
        if not authorized:
            raise ModelRouteError(
                "model.route_not_allowed", "snapshot does not authorize a route chain"
            )
        raw_routes = target.get("routes")
        if not isinstance(raw_routes, list):
            raise ModelRouteError("budget.reservation_rejected", "snapshot routes are invalid")
        frozen_by_ref: dict[tuple[str, str], FrozenModelRouteSnapshot] = {}
        for route_ref in authorized:
            matching: list[dict[str, object]] = []
            for raw_route in cast(list[object], raw_routes):
                if not isinstance(raw_route, dict):
                    continue
                item = cast(dict[str, object], raw_route)
                if (
                    item.get("usage_kind") == "model"
                    and item.get("deployment_id") == route_ref.deployment_id
                    and item.get("model") == route_ref.model_id
                ):
                    matching.append(item)
            if len(matching) != 1:
                raise ModelRouteError(
                    "budget.reservation_rejected", "snapshot chain route is ambiguous"
                )
            try:
                frozen_by_ref[(route_ref.deployment_id, route_ref.model_id)] = (
                    FrozenModelRouteSnapshot.model_validate(matching[0])
                )
            except Exception as exc:
                raise ModelRouteError(
                    "budget.reservation_rejected", "snapshot chain route is incomplete"
                ) from exc
        first = authorized[0]
        first_frozen = frozen_by_ref[(first.deployment_id, first.model_id)]
        projected_models = list(
            dict.fromkeys(
                item.model_id for item in authorized if item.deployment_id == first.deployment_id
            )
        )
        if (
            policy.deployment_id != first.deployment_id
            or policy.provider != first_frozen.provider
            or policy.allowed_models != projected_models
            or policy.default_model != first.model_id
            or policy.fallback_models
        ):
            raise ModelRouteError(
                "budget.reservation_rejected", "snapshot chain projection is invalid"
            )

        selected = self._select_snapshot_chain_refs(
            request,
            authorized=authorized,
            frozen_by_ref=frozen_by_ref,
        )
        candidates: list[ModelRouteCandidate] = []
        for ordinal, route_ref in enumerate(selected, start=1):
            frozen = frozen_by_ref[(route_ref.deployment_id, route_ref.model_id)]
            static_ineligible_cause: Literal["capability", "input_bound", "hard_budget"] | None = (
                None
            )
            if request.capability not in frozen.capabilities:
                static_ineligible_cause = "capability"
            if request.max_output_tokens > frozen.max_output_tokens or (
                len(request.prompt.encode("utf-8")) > frozen.max_prompt_utf8_bytes
            ):
                static_ineligible_cause = static_ineligible_cause or "input_bound"
            narrow_policy = policy.model_copy(
                update={
                    "deployment_id": frozen.deployment_id,
                    "provider": frozen.provider,
                    "allowed_models": [frozen.model],
                    "default_model": frozen.model,
                    "fallback_models": [],
                }
            )
            route = self._plan_frozen_route(
                request.model_copy(
                    update={
                        "deployment_id": frozen.deployment_id,
                        "provider": frozen.provider,
                        "model": frozen.model,
                        "route_refs": None,
                    }
                ),
                policy=narrow_policy,
                frozen=frozen,
                tolerate_static_ineligible=True,
            )
            hard_token_limit, hard_cost_limit = self._snapshot_target_budget(target)
            if self._over_hard_budget(
                route,
                token_limit=hard_token_limit,
                cost_limit=hard_cost_limit,
            ):
                static_ineligible_cause = static_ineligible_cause or "hard_budget"
            if self._over_soft_budget(
                route,
                token_limit=frozen.soft_max_tokens_per_call,
                cost_limit=None,
            ):
                route = self._with_route_decision(
                    route,
                    action="policy_required",
                    reason="candidate exceeds frozen soft budget threshold",
                    approval_kind="soft_budget",
                    token_limit=self._minimum_limit(
                        frozen.soft_max_tokens_per_call,
                        hard_token_limit,
                    ),
                    cost_limit=hard_cost_limit,
                )
            else:
                route = self._with_route_decision(
                    route,
                    action="call",
                    token_limit=self._minimum_limit(
                        frozen.soft_max_tokens_per_call,
                        hard_token_limit,
                    ),
                    cost_limit=hard_cost_limit,
                )
            candidate = ModelRouteCandidate(
                ordinal=ordinal,
                deployment_id=frozen.deployment_id,
                provider=frozen.provider,
                model=frozen.model,
                route_digest=model_route_digest(route_plan_identity_payload(route)),
                endpoint_policy_digest=frozen.endpoint_policy_digest,
                model_catalog_digest=frozen.model_catalog_digest,
                retry_policy_digest=model_route_digest(
                    {
                        "schema_version": "model-route-retry-policy-v1",
                        **route.retry_policy.model_dump(mode="python"),
                    }
                ),
                bulkhead_policy_digest=model_route_digest(
                    {
                        "schema_version": "model-route-bulkhead-policy-v1",
                        **route.bulkhead_policy.model_dump(mode="python"),
                    }
                ),
                credential_ref=frozen.credential_ref,
                model_catalog_ref=frozen.model_catalog_ref,
                model_catalog_version=frozen.model_catalog_version,
                reserved_token_bound=route.reserved_token_bound,
                reserved_cost_bound=route.reserved_cost_bound,
                static_ineligible_cause=static_ineligible_cause,
                route=route,
            )
            candidates.append(candidate)
        preimage: dict[str, object] = {
            "schema_version": "model-route-chain-id-v1",
            "capability": request.capability,
            "candidate_count": len(candidates),
            "agent_model_policy": {
                "deployment_id": policy.deployment_id,
                "provider": policy.provider,
                "allowed_models": list(policy.allowed_models),
                "default_model": policy.default_model,
                "fallback_models": list(policy.fallback_models),
                "fallback_routes": [
                    {
                        "deployment_id": item.deployment_id,
                        "model_id": item.model_id,
                    }
                    for item in authorized
                ],
            },
            "request_bounds": {
                "prompt_utf8_bytes": len(request.prompt.encode("utf-8")),
                "max_output_tokens": request.max_output_tokens,
            },
            "candidates": [self._candidate_identity_payload(item) for item in candidates],
        }
        return ModelRouteChainPlan(
            chain_id=model_route_digest(preimage),
            capability=cast(Literal["text_completion", "text_stream"], request.capability),
            candidate_count=len(candidates),
            agent_model_policy=ModelRouteAgentPolicyIdentity.model_validate(
                preimage["agent_model_policy"]
            ),
            request_bounds=ModelRouteRequestBounds.model_validate(preimage["request_bounds"]),
            candidates=tuple(candidates),
        )

    @staticmethod
    def _select_snapshot_chain_refs(
        request: ModelRequest,
        *,
        authorized: list[ModelRouteRef],
        frozen_by_ref: Mapping[tuple[str, str], FrozenModelRouteSnapshot],
    ) -> list[ModelRouteRef]:
        """按冻结 refs 验证 request 有序子序列和兼容单值断言。"""

        selected = list(request.route_refs) if request.route_refs is not None else list(authorized)
        if not selected or len(selected) != len(set(selected)):
            raise ModelRouteError("model.route_not_allowed", "request route refs are invalid")
        cursor = 0
        for route_ref in selected:
            while cursor < len(authorized) and authorized[cursor] != route_ref:
                cursor += 1
            if cursor == len(authorized):
                raise ModelRouteError(
                    "model.route_not_allowed", "request route refs are not an ordered subsequence"
                )
            cursor += 1
        legacy_fields = (request.deployment_id, request.provider, request.model)
        if any(value is not None for value in legacy_fields):
            matches = [
                item
                for item in selected
                if (request.deployment_id is None or request.deployment_id == item.deployment_id)
                and (request.model is None or request.model == item.model_id)
                and (
                    request.provider is None
                    or request.provider
                    == frozen_by_ref[(item.deployment_id, item.model_id)].provider
                )
            ]
            if len(matches) != 1 or (
                request.route_refs is not None and (len(selected) != 1 or selected[0] != matches[0])
            ):
                raise ModelRouteError(
                    "model.route_not_allowed", "legacy request fields must select one route"
                )
            selected = matches
        return selected

    def _plan_frozen_route(
        self,
        request: ModelRequest,
        *,
        policy: AgentModelPolicyLike,
        frozen: FrozenModelRouteSnapshot,
        tolerate_static_ineligible: bool = False,
        tool_catalog: ToolCatalog | None = None,
    ) -> ModelRoutePlan:
        """对快照静态输入重做动态 hard eligibility 与 checked reservation 公式。"""

        provider = self._providers.get(frozen.deployment_id) or self._providers.get(frozen.provider)
        if provider is None or provider.provider_id != frozen.provider:
            raise ModelRouteError("model.route_not_allowed", "bound provider identity mismatch")
        if request.capability not in frozen.capabilities and not tolerate_static_ineligible:
            raise ModelRouteError("model.capability_unsupported", "snapshot capability mismatch")
        if request.max_output_tokens < 1 or (
            request.max_output_tokens > frozen.max_output_tokens and not tolerate_static_ineligible
        ):
            raise ModelRouteError("model.route_not_allowed", "output cap cannot exceed snapshot")
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if prompt_bytes > frozen.max_prompt_utf8_bytes and not tolerate_static_ineligible:
            raise ModelRouteError("model.route_not_allowed", "prompt exceeds snapshot byte cap")
        tool_mode = request.capability == "tool_intent"
        if tool_mode != (tool_catalog is not None):
            raise ModelRouteError(
                "model.tool_catalog_conflict",
                "snapshot route and tool catalog mode mismatch",
            )
        if tool_mode:
            if (
                frozen.request_shape_ref != "single-user-text-with-tool-catalog"
                or frozen.max_tool_catalog_utf8_bytes is None
            ):
                raise ModelRouteError(
                    "budget.reservation_rejected",
                    "tool-intent snapshot catalog identity is incomplete",
                )
            assert tool_catalog is not None
            provider_catalog = provider_tool_catalog_bytes(tool_catalog)
            if len(provider_catalog) > frozen.max_tool_catalog_utf8_bytes:
                raise ModelRouteError(
                    "model.input_too_large",
                    "provider tool catalog exceeds frozen byte cap",
                )
        else:
            if (
                frozen.request_shape_ref != "single-user-text-no-tools"
                or frozen.max_tool_catalog_utf8_bytes is not None
            ):
                raise ModelRouteError(
                    "budget.reservation_rejected",
                    "no-tools snapshot carries tool catalog identity",
                )
            provider_catalog = b""
        expected_static_tokens = (
            frozen.max_prompt_utf8_bytes
            + (frozen.max_tool_catalog_utf8_bytes or 0)
            + frozen.input_envelope_token_bound
            + frozen.max_output_tokens
        )
        if expected_static_tokens != frozen.max_per_attempt_token_bound:
            raise ModelRouteError("budget.reservation_rejected", "snapshot token formula mismatch")
        trusted_input = prompt_bytes + len(provider_catalog) + frozen.input_envelope_token_bound
        per_attempt_tokens = trusted_input + request.max_output_tokens
        if (
            per_attempt_tokens > frozen.max_per_attempt_token_bound
            and not tolerate_static_ineligible
        ):
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
                Decimal(
                    frozen.max_prompt_utf8_bytes
                    + (frozen.max_tool_catalog_utf8_bytes or 0)
                    + frozen.input_envelope_token_bound
                )
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
            estimated_cost_usd=per_attempt_cost,
            price_source_ref=frozen.price_source_ref,
            price_source_version=frozen.price_source_version,
        )
        tool_request_identity = (
            ToolIntentRequestIdentity(
                model_catalog_digest=frozen.model_catalog_digest,
                tool_catalog_digest=tool_catalog.catalog_digest,
                tool_catalog_utf8_bytes=len(provider_catalog),
                max_tool_catalog_utf8_bytes=frozen.max_tool_catalog_utf8_bytes,
                trusted_input_token_bound=trusted_input,
                output_token_cap=request.max_output_tokens,
            )
            if tool_catalog is not None and frozen.max_tool_catalog_utf8_bytes is not None
            else None
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
            tool_request_identity=tool_request_identity,
            tool_request_identity_digest=(
                tool_request_identity.digest if tool_request_identity is not None else None
            ),
            provider_tool_catalog_json=(
                provider_catalog.decode("utf-8") if tool_request_identity is not None else None
            ),
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
            max_structured_repair_attempts=frozen.max_structured_repair_attempts,
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
            cross_provider_failover_http_statuses=(frozen.cross_provider_failover_http_statuses),
            bulkhead_policy=ModelBulkheadPolicy.model_validate(frozen.bulkhead_policy),
            snapshot_schema_version="budget-tree-v2",
            trusted_token_bound=per_attempt_tokens * frozen.max_attempts,
            trusted_cost_bound=reserved_cost,
        )
