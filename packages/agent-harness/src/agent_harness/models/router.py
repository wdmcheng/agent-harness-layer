"""模型路由、预算估算和 reload seam。"""

from __future__ import annotations

from collections.abc import Mapping

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


class ModelRoutePlan(HarnessDTO):
    """provider 副作用前冻结的实际路由与预算判断。"""

    provider: str
    model: str
    decision: ModelDecision


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
        self.config = config
        self._providers = dict(providers)

    def reload(self, config: ModelRouterConfig) -> None:
        """显式 reload seam；P0 不做 worker 运行中自动热重载。"""

        self.config = config

    def route(self, request: ModelRequest) -> ModelResponse:
        """兼容既有同步调用；usage evidence 由受控 async seam 负责。"""

        plan = self.plan(request)
        return self.execute(request, plan=plan)

    def plan(self, request: ModelRequest) -> ModelRoutePlan:
        """在 provider 调用前确定实际 provider/model 与零副作用拒绝。"""

        provider_id = request.provider or self.config.default_provider
        if provider_id not in self._providers:
            raise KeyError(f"model provider is not configured: {provider_id}")
        estimated_tokens = request.estimated_input_tokens + request.max_output_tokens
        selected_model = request.model or self.config.default_model
        action = "call"
        fallback_model = None
        reason = None
        if (
            self.config.max_tokens_per_call is not None
            and estimated_tokens > self.config.max_tokens_per_call
        ):
            # 超预算先尝试 fallback；没有 fallback 才把判断交给 policy seam。
            # 这里不抛异常，是为了让调用方保留可审计的 decision summary。
            if self.config.fallback_models:
                action = "fallback"
                fallback_model = self.config.fallback_models[0]
                selected_model = fallback_model
                reason = "estimated tokens exceed budget"
            else:
                action = "policy_required"
                reason = "estimated tokens exceed budget and no fallback is configured"
        decision = ModelDecision(
            action=action,
            estimated_tokens=estimated_tokens,
            max_tokens=self.config.max_tokens_per_call,
            fallback_model=fallback_model,
            reason=reason,
        )
        return ModelRoutePlan(
            provider=provider_id,
            model=selected_model,
            decision=decision,
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
