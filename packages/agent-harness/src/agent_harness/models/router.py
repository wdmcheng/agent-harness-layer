"""模型路由、预算估算和 reload seam。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelDecision, ModelProvider, ModelRequest, ModelResponse


class ModelRouterConfig(HarnessDTO):
    default_provider: str = "fake"
    default_model: str
    fallback_models: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
    max_tokens_per_call: int | None = None


class ModelRouter:
    """根据 provider/model/budget 配置选择模型。"""

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
        provider_id = request.provider or self.config.default_provider
        provider = self._providers[provider_id]
        estimated_tokens = request.estimated_input_tokens + request.max_output_tokens
        selected_model = request.model or self.config.default_model
        action = "call"
        fallback_model = None
        reason = None
        if (
            self.config.max_tokens_per_call is not None
            and estimated_tokens > self.config.max_tokens_per_call
        ):
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
        if action == "policy_required":
            return ModelResponse(
                provider=provider_id,
                model=selected_model,
                output_text="",
                decision=decision,
                token_usage={
                    "input_tokens": request.estimated_input_tokens,
                    "output_tokens": 0,
                },
            )
        routed_request = request.model_copy(
            update={"timeout_seconds": request.timeout_seconds or self.config.timeout_seconds}
        )
        response = provider.complete(routed_request, model=selected_model)
        if response.decision.action != "call":
            return response
        return response.model_copy(update={"decision": decision})
