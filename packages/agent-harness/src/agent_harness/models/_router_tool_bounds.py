"""工具感知路由的可信输入、成本上界与请求身份计算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agent_harness.config.model_catalog import checked_budget_add, checked_budget_mul
from agent_harness.config.model_endpoints import ResolvedModelDeployment
from agent_harness.config.schemas import ModelCatalogEntrySettings, ModelDeploymentSettings
from agent_harness.models._router_contracts import ModelRouteError
from agent_harness.models.providers import ModelRequest
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolIntentRequestIdentity,
    provider_tool_catalog_bytes,
)


@dataclass(frozen=True)
class ToolAwareRouteBounds:
    """单个真实 provider 候选已经证明的冻结预算与工具目录身份。"""

    provider_catalog: bytes
    trusted_input_tokens: int
    per_attempt_tokens: int
    reserved_tokens: int
    per_attempt_cost: Decimal | None
    reserved_cost: Decimal | None
    tool_request_identity: ToolIntentRequestIdentity | None
    tool_request_identity_digest: str | None


def build_tool_aware_route_bounds(
    *,
    request: ModelRequest,
    prompt_bytes: int,
    deployment: ModelDeploymentSettings,
    catalog: ModelCatalogEntrySettings,
    resolved: ResolvedModelDeployment,
    tool_catalog: ToolCatalog | None,
    tolerate_input_bound: bool,
) -> ToolAwareRouteBounds:
    """在路由 plan 创建前一次性证明工具目录、token 与 cost 静态/动态上界。"""

    provider_catalog = b""
    if request.capability == "tool_intent":
        if tool_catalog is None or catalog.max_tool_catalog_utf8_bytes is None:
            raise ModelRouteError(
                "model.tool_catalog_conflict",
                "tool-intent route is missing its frozen catalog",
            )
        provider_catalog = provider_tool_catalog_bytes(tool_catalog)
        if len(provider_catalog) > catalog.max_tool_catalog_utf8_bytes:
            raise ModelRouteError(
                "model.tool_catalog_conflict",
                "provider tool catalog exceeds model catalog bound",
            )
    catalog_bytes = len(provider_catalog)
    catalog_max = catalog.max_tool_catalog_utf8_bytes or 0
    try:
        static_token_ceiling = checked_budget_add(
            deployment.max_prompt_utf8_bytes,
            catalog_max,
            catalog.input_envelope_token_bound,
            deployment.max_output_tokens,
        )
        trusted_input = checked_budget_add(
            prompt_bytes,
            catalog_bytes,
            catalog.input_envelope_token_bound,
        )
        per_attempt_tokens = checked_budget_add(trusted_input, request.max_output_tokens)
        reserved_tokens = checked_budget_mul(per_attempt_tokens, deployment.max_attempts)
    except ValueError:
        raise ModelRouteError(
            "budget.reservation_rejected",
            "tool-aware token formula overflow",
        ) from None
    if static_token_ceiling > resolved.max_per_attempt_token_bound:
        raise ModelRouteError(
            "budget.reservation_rejected", "candidate token formula exceeds deployment"
        )
    if per_attempt_tokens > static_token_ceiling and not tolerate_input_bound:
        raise ModelRouteError(
            "budget.reservation_rejected", "dynamic token bound exceeds static ceiling"
        )

    per_attempt_cost, reserved_cost = _cost_bounds(
        request=request,
        deployment=deployment,
        catalog=catalog,
        resolved=resolved,
        catalog_max=catalog_max,
        trusted_input=trusted_input,
    )
    tool_request_identity: ToolIntentRequestIdentity | None = None
    tool_request_identity_digest: str | None = None
    if request.capability == "tool_intent":
        assert tool_catalog is not None and catalog.max_tool_catalog_utf8_bytes is not None
        assert catalog.digest is not None
        tool_request_identity = ToolIntentRequestIdentity(
            model_catalog_digest=catalog.digest,
            tool_catalog_digest=tool_catalog.catalog_digest,
            tool_catalog_utf8_bytes=catalog_bytes,
            max_tool_catalog_utf8_bytes=catalog.max_tool_catalog_utf8_bytes,
            trusted_input_token_bound=trusted_input,
            output_token_cap=request.max_output_tokens,
        )
        tool_request_identity_digest = tool_request_identity.digest
    return ToolAwareRouteBounds(
        provider_catalog=provider_catalog,
        trusted_input_tokens=trusted_input,
        per_attempt_tokens=per_attempt_tokens,
        reserved_tokens=reserved_tokens,
        per_attempt_cost=per_attempt_cost,
        reserved_cost=reserved_cost,
        tool_request_identity=tool_request_identity,
        tool_request_identity_digest=tool_request_identity_digest,
    )


def _cost_bounds(
    *,
    request: ModelRequest,
    deployment: ModelDeploymentSettings,
    catalog: ModelCatalogEntrySettings,
    resolved: ResolvedModelDeployment,
    catalog_max: int,
    trusted_input: int,
) -> tuple[Decimal | None, Decimal | None]:
    """对启用计费的 catalog 证明单次与完整 attempt 预约成本。"""

    if not catalog.cost_enabled:
        return None, None
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
        Decimal(deployment.max_prompt_utf8_bytes + catalog_max + catalog.input_envelope_token_bound)
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
    return per_attempt_cost, per_attempt_cost * deployment.max_attempts


__all__ = ["ToolAwareRouteBounds", "build_tool_aware_route_bounds"]
