"""Provider-neutral model request / response DTO。"""

from __future__ import annotations

from typing import Protocol

from agent_harness.contracts.dto import HarnessDTO


class ModelRequest(HarnessDTO):
    provider: str = "fake"
    prompt: str
    model: str | None = None
    estimated_input_tokens: int = 0
    max_output_tokens: int = 0
    timeout_seconds: int | None = None


class ModelDecision(HarnessDTO):
    action: str
    estimated_tokens: int
    max_tokens: int | None = None
    fallback_model: str | None = None
    reason: str | None = None


class ModelResponse(HarnessDTO):
    provider: str
    model: str
    output_text: str
    decision: ModelDecision
    token_usage: dict[str, int]
    latency_ms: int = 0


class ModelProvider(Protocol):
    provider_id: str

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        """执行一次模型调用，provider SDK 细节留在 adapter 后面。"""
        ...
