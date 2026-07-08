"""离线 fake model provider。"""

from __future__ import annotations

from agent_harness.models.providers import ModelDecision, ModelRequest, ModelResponse


class FakeModelProvider:
    """测试和 local smoke 默认使用的确定性 provider。"""

    provider_id = "fake"

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
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
