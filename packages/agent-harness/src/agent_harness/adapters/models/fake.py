"""离线 fake model provider。"""

from __future__ import annotations

from typing import cast

from agent_harness.models.providers import ModelDecision, ModelRequest, ModelResponse
from agent_harness.models.router import ModelRoutePlan


class FakeModelProvider:
    """测试和 local smoke 默认使用的确定性 provider。"""

    provider_id = "fake"

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """返回确定性文本，供 local smoke 和 contract tests 不依赖外部 provider。"""

        model = cast(ModelRoutePlan, plan).model
        output = f"fake:{request.prompt}"
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text=output,
            decision=ModelDecision(
                action="call",
                estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
            ),
            token_usage={
                "input_tokens": request.estimated_input_tokens,
                "output_tokens": min(request.max_output_tokens, len(output.split())),
            },
            latency_ms=0,
        )

    async def aclose(self) -> None:
        """Fake 不持有外部资源；保留统一的 provider 生命周期协议。"""
