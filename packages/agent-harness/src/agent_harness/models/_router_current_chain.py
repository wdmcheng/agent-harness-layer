"""从 _router_current.py 拆出的私有职责模块；公共 façade 与顺序语义保持不变。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, cast

from agent_harness.config.model_endpoints import (
    ResolvedModelDeployment,
    resolve_model_deployment,
)
from agent_harness.config.schemas import ModelRouteRef, ModelSettings
from agent_harness.models._router_contracts import (
    AgentModelPolicyLike,
    ModelRouteAgentPolicyIdentity,
    ModelRouteCandidate,
    ModelRouteChainPlan,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouterConfig,
    ModelRouteRequestBounds,
)
from agent_harness.models._router_identity import (
    canonical_decimal,
    model_route_digest,
    route_plan_identity_payload,
)
from agent_harness.models.providers import ModelProvider, ModelRequest


class RouterCurrentChainPlanningMixin:
    """承载从兼容入口拆出的单一私有职责。"""

    config: ModelRouterConfig
    _providers: dict[str, ModelProvider]
    _model_settings: ModelSettings | None

    def _build_current_route_plan(
        self,
        request: ModelRequest,
        *,
        deployment_id: str,
        allowed: tuple[str, ...],
        resolved: ResolvedModelDeployment,
        tolerate_input_bound: bool = False,
    ) -> ModelRoutePlan:
        """由 current-planning 子类提供单候选冻结计划。"""

        raise NotImplementedError

    @staticmethod
    def _over_soft_budget(
        plan: ModelRoutePlan,
        *,
        token_limit: int | None,
        cost_limit: Decimal | None,
    ) -> bool:
        """由 current-planning 子类提供调用级预算比较。"""

        raise NotImplementedError

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
        """由 current-planning 子类提供不可变 decision 替换。"""

        raise NotImplementedError

    def _plan_controlled_chain(
        self,
        request: ModelRequest,
        *,
        agent_policy: AgentModelPolicyLike,
    ) -> ModelRouteChainPlan:
        """按 Agent 最大授权与请求有序子序列冻结所有候选。"""

        model_settings = self._model_settings
        assert model_settings is not None
        authorized = list(agent_policy.fallback_routes)
        if not authorized:
            raise ModelRouteError(
                "model.route_not_allowed", "explicit route chain requires fallback routes"
            )
        if len(authorized) > 8 or len(authorized) != len(set(authorized)):
            raise ModelRouteError("model.route_not_allowed", "route chain is invalid")

        first = authorized[0]
        try:
            first_resolved = resolve_model_deployment(model_settings, first.deployment_id)
        except ValueError as exc:
            raise ModelRouteError(
                "model.route_not_allowed", "first route deployment is not available"
            ) from exc
        projected_models = list(
            dict.fromkeys(
                route.model_id for route in authorized if route.deployment_id == first.deployment_id
            )
        )
        if (
            agent_policy.deployment_id != first.deployment_id
            or agent_policy.provider != first_resolved.provider_kind
            or agent_policy.allowed_models != projected_models
            or agent_policy.default_model != first.model_id
            or agent_policy.fallback_models
        ):
            raise ModelRouteError(
                "model.route_not_allowed", "legacy fields do not match first route projection"
            )

        selected = self._select_chain_refs(
            request,
            authorized=authorized,
            model_settings=model_settings,
        )
        candidates = tuple(
            self._build_chain_candidate(request, ordinal=ordinal, route_ref=route_ref)
            for ordinal, route_ref in enumerate(selected, start=1)
        )
        candidate_identities = [self._candidate_identity_payload(item) for item in candidates]
        preimage: dict[str, object] = {
            "schema_version": "model-route-chain-id-v1",
            "capability": request.capability,
            "candidate_count": len(candidates),
            "agent_model_policy": {
                "deployment_id": agent_policy.deployment_id,
                "provider": agent_policy.provider,
                "allowed_models": list(agent_policy.allowed_models),
                "default_model": agent_policy.default_model,
                "fallback_models": list(agent_policy.fallback_models),
                "fallback_routes": [
                    {
                        "deployment_id": route.deployment_id,
                        "model_id": route.model_id,
                    }
                    for route in authorized
                ],
            },
            "request_bounds": {
                "prompt_utf8_bytes": len(request.prompt.encode("utf-8")),
                "max_output_tokens": request.max_output_tokens,
            },
            "candidates": candidate_identities,
        }
        return ModelRouteChainPlan(
            chain_id=model_route_digest(preimage),
            capability=cast(Literal["text_completion", "text_stream"], request.capability),
            candidate_count=len(candidates),
            agent_model_policy=ModelRouteAgentPolicyIdentity.model_validate(
                preimage["agent_model_policy"]
            ),
            request_bounds=ModelRouteRequestBounds.model_validate(preimage["request_bounds"]),
            candidates=candidates,
        )

    def _select_chain_refs(
        self,
        request: ModelRequest,
        *,
        authorized: list[ModelRouteRef],
        model_settings: ModelSettings,
    ) -> list[ModelRouteRef]:
        """只允许请求删除候选；兼容单值字段必须唯一收窄到同一个 ref。"""

        requested = list(request.route_refs) if request.route_refs is not None else None
        if requested is not None:
            if not requested or len(requested) != len(set(requested)):
                raise ModelRouteError(
                    "model.route_not_allowed", "request route refs must be nonempty and unique"
                )
            cursor = 0
            for route in requested:
                while cursor < len(authorized) and authorized[cursor] != route:
                    cursor += 1
                if cursor == len(authorized):
                    raise ModelRouteError(
                        "model.route_not_allowed",
                        "request route refs must be an ordered authorization subsequence",
                    )
                cursor += 1
            selected = requested
        else:
            selected = list(authorized)

        legacy_fields = (request.deployment_id, request.provider, request.model)
        if any(value is not None for value in legacy_fields):
            matches: list[ModelRouteRef] = []
            for route in selected:
                deployment_id = route.deployment_id
                model_id = route.model_id
                try:
                    provider = resolve_model_deployment(model_settings, deployment_id).provider_kind
                except ValueError as exc:
                    raise ModelRouteError(
                        "model.route_not_allowed", "request references an unavailable route"
                    ) from exc
                if (
                    (request.deployment_id is None or request.deployment_id == deployment_id)
                    and (request.provider is None or request.provider == provider)
                    and (request.model is None or request.model == model_id)
                ):
                    matches.append(route)
            if len(matches) != 1:
                raise ModelRouteError(
                    "model.route_not_allowed", "legacy request fields must select one route"
                )
            if requested is not None and (len(selected) != 1 or selected[0] != matches[0]):
                raise ModelRouteError(
                    "model.route_not_allowed", "legacy request fields conflict with route refs"
                )
            selected = matches
        return selected

    def _build_chain_candidate(
        self,
        request: ModelRequest,
        *,
        ordinal: int,
        route_ref: ModelRouteRef,
    ) -> ModelRouteCandidate:
        """逐 deployment 解析安全边界并复用现有可信上界公式。"""

        model_settings = self._model_settings
        assert model_settings is not None
        deployment_id = route_ref.deployment_id
        model_id = route_ref.model_id
        try:
            resolved = resolve_model_deployment(model_settings, deployment_id)
        except ValueError as exc:
            raise ModelRouteError("model.route_not_allowed", "deployment is not available") from exc
        deployment = model_settings.deployments[deployment_id]
        if model_id not in resolved.allowed_models:
            raise ModelRouteError("model.route_not_allowed", "route model is not allowed")
        if request.capability not in {"text_completion", "text_stream"}:
            raise ModelRouteError("model.capability_unsupported", "capability is not supported")
        static_ineligible_cause: Literal["capability", "input_bound"] | None = None
        if request.capability not in deployment.capabilities:
            static_ineligible_cause = "capability"
        provider = self._providers.get(deployment_id) or self._providers.get(resolved.provider_kind)
        if provider is None or provider.provider_id != resolved.provider_kind:
            raise ModelRouteError("model.route_not_allowed", "bound provider identity mismatch")
        if request.max_output_tokens > deployment.max_output_tokens or (
            len(request.prompt.encode("utf-8")) > deployment.max_prompt_utf8_bytes
        ):
            static_ineligible_cause = static_ineligible_cause or "input_bound"
        route = self._build_current_route_plan(
            request.model_copy(
                update={
                    "deployment_id": deployment_id,
                    "provider": resolved.provider_kind,
                    "model": model_id,
                    "route_refs": None,
                }
            ),
            deployment_id=deployment_id,
            allowed=(model_id,),
            resolved=resolved,
            tolerate_input_bound=True,
        )
        token_limit = self.config.route_max_tokens_per_call.get(
            model_id, self.config.max_tokens_per_call
        )
        cost_limit = self.config.route_max_cost_per_call.get(
            model_id, self.config.max_cost_per_call
        )
        if self._over_soft_budget(route, token_limit=token_limit, cost_limit=cost_limit):
            route = self._with_route_decision(
                route,
                action="policy_required",
                reason="candidate exceeds configured soft budget threshold",
                approval_kind="soft_budget",
                token_limit=token_limit,
                cost_limit=cost_limit,
            )
        else:
            route = self._with_route_decision(
                route,
                action="call",
                token_limit=token_limit,
                cost_limit=cost_limit,
            )
        retry_digest = model_route_digest(
            {
                "schema_version": "model-route-retry-policy-v1",
                **route.retry_policy.model_dump(mode="python"),
            }
        )
        bulkhead_digest = model_route_digest(
            {
                "schema_version": "model-route-bulkhead-policy-v1",
                **route.bulkhead_policy.model_dump(mode="python"),
            }
        )
        endpoint_digest = route.endpoint_policy_digest or model_route_digest(
            {
                "schema_version": "model-route-local-endpoint-v1",
                "deployment_id": deployment_id,
            }
        )
        catalog_digest = route.model_catalog_digest or model_route_digest(
            {
                "schema_version": "model-route-local-catalog-v1",
                "deployment_id": deployment_id,
                "model": model_id,
            }
        )
        return ModelRouteCandidate(
            ordinal=ordinal,
            deployment_id=deployment_id,
            provider=resolved.provider_kind,
            model=model_id,
            route_digest=model_route_digest(route_plan_identity_payload(route)),
            endpoint_policy_digest=endpoint_digest,
            model_catalog_digest=catalog_digest,
            retry_policy_digest=retry_digest,
            bulkhead_policy_digest=bulkhead_digest,
            credential_ref=route.credential_ref,
            model_catalog_ref=route.model_catalog_ref or "local",
            model_catalog_version=route.model_catalog_version or "v1",
            reserved_token_bound=route.reserved_token_bound,
            reserved_cost_bound=route.reserved_cost_bound,
            static_ineligible_cause=static_ineligible_cause,
            route=route,
        )

    @staticmethod
    def _candidate_identity_payload(candidate: ModelRouteCandidate) -> dict[str, object]:
        """生成 chain-id preimage 中 exact candidate object。"""

        return {
            "ordinal": candidate.ordinal,
            "deployment_id": candidate.deployment_id,
            "provider": candidate.provider,
            "model": candidate.model,
            "route_digest": candidate.route_digest,
            "endpoint_policy_digest": candidate.endpoint_policy_digest,
            "model_catalog_digest": candidate.model_catalog_digest,
            "retry_policy_digest": candidate.retry_policy_digest,
            "bulkhead_policy_digest": candidate.bulkhead_policy_digest,
            "credential_ref": candidate.credential_ref,
            "model_catalog_ref": candidate.model_catalog_ref,
            "model_catalog_version": candidate.model_catalog_version,
            "reserved_token_bound": candidate.reserved_token_bound,
            "reserved_cost_bound": (
                None
                if candidate.reserved_cost_bound is None
                else canonical_decimal(candidate.reserved_cost_bound)
            ),
        }
