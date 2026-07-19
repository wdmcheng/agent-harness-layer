"""模型路由、预算估算和 reload seam。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelDecision, ModelProvider, ModelRequest, ModelResponse


class ModelRouterConfig(HarnessDTO):
    """模型路由的运行时配置，支持显式 reload。"""

    default_provider: str = "fake"
    default_model: str
    fallback_models: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
    max_tokens_per_call: int | None = None
    input_token_price_usd: Decimal | None = None
    output_token_price_usd: Decimal | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None
    route_price_source_refs: dict[str, str] = Field(default_factory=dict)
    route_price_source_versions: dict[str, str] = Field(default_factory=dict)
    route_input_token_prices_usd: dict[str, Decimal] = Field(default_factory=dict)
    route_output_token_prices_usd: dict[str, Decimal] = Field(default_factory=dict)
    route_max_tokens_per_call: dict[str, int] = Field(default_factory=dict)


class ModelRoutePlan(HarnessDTO):
    """provider 副作用前冻结的实际路由与预算判断。"""

    provider: str
    model: str
    decision: ModelDecision
    trusted_token_bound: int
    trusted_cost_bound: Decimal | None


class ModelRouter:
    """根据 provider/model/budget 配置选择模型。

    Router 只产出 provider-neutral decision，不直接触发 PolicyEngine。后续
    runtime、tool 或 eval seam 可以根据 `policy_required` 决策进入审批或降级。
    """

    def __init__(
        self,
        *,
        config: ModelRouterConfig,
        providers: Mapping[str, ModelProvider],
    ) -> None:
        """冻结当前路由配置并复制 provider 映射，隔离调用方后续字典修改。"""

        self.config = config
        self._providers = dict(providers)

    def reload(self, config: ModelRouterConfig) -> None:
        """显式 reload seam；当前实现不做 worker 运行中的自动热重载。"""

        self.config = config

    def route(self, request: ModelRequest) -> ModelResponse:
        """兼容既有同步调用；usage evidence 由受控 async seam 负责。"""

        plan = self.plan(request)
        return self.execute(request, plan=plan)

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        approved: bool = False,
    ) -> ModelRoutePlan:
        """在 provider 调用前确定实际 provider/model 与零副作用拒绝。"""

        active = config or self.config
        provider_id = request.provider or active.default_provider
        if provider_id not in self._providers:
            raise KeyError(f"model provider is not configured: {provider_id}")
        # UTF-8 字节数是保守上界：受支持 tokenizer 的计费 token 数不会超过字节数。
        # 调用方估算仅作为 evidence，绝不能缩小 provider 强制执行的这个边界。
        trusted_input_bound = len(request.prompt.encode("utf-8"))
        estimated_tokens = trusted_input_bound + request.max_output_tokens
        selected_model = request.model or active.default_model
        action = "call"
        fallback_model = None
        reason = None
        if (
            not approved
            and active.max_tokens_per_call is not None
            and estimated_tokens > active.max_tokens_per_call
        ):
            # 每个 fallback 都必须重新形成 route intent 并重新过 soft threshold。
            # 未声明更高的 route-specific threshold 时沿用当前阈值，因此不能
            # 仅通过更换 model 名称绕过同一个 soft gate。
            fallback_model = next(
                (
                    model
                    for model in active.fallback_models
                    if estimated_tokens
                    <= active.route_max_tokens_per_call.get(
                        model,
                        active.max_tokens_per_call,
                    )
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
        input_token_price = active.route_input_token_prices_usd.get(
            selected_model, active.input_token_price_usd
        )
        output_token_price = active.route_output_token_prices_usd.get(
            selected_model, active.output_token_price_usd
        )
        trusted_cost_bound = None
        if input_token_price is not None and output_token_price is not None:
            trusted_cost_bound = (
                Decimal(trusted_input_bound) * input_token_price
                + Decimal(request.max_output_tokens) * output_token_price
            )
        return ModelRoutePlan(
            provider=provider_id,
            model=selected_model,
            decision=decision,
            trusted_token_bound=estimated_tokens,
            trusted_cost_bound=trusted_cost_bound,
        )

    def execute(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """执行已冻结 plan；调用方必须先完成 evidence 预约。"""

        if plan.decision.action == "policy_required":
            return ModelResponse(
                provider=plan.provider,
                model=plan.model,
                output_text="",
                decision=plan.decision,
                token_usage={},
            )
        provider = self._providers[plan.provider]
        routed_request = request.model_copy(
            update={"timeout_seconds": request.timeout_seconds or self.config.timeout_seconds}
        )
        response = provider.complete(routed_request, model=plan.model)
        if response.decision.action != "call":
            return response
        normalized_decision = response.decision.model_copy(
            update={
                "action": plan.decision.action,
                "estimated_tokens": plan.decision.estimated_tokens,
                "max_tokens": plan.decision.max_tokens,
                "fallback_model": plan.decision.fallback_model,
                "reason": plan.decision.reason or response.decision.reason,
            }
        )
        return response.model_copy(update={"decision": normalized_decision})
