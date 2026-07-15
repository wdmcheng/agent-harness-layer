"""模型 provider 无关的请求、响应与决策 DTO。"""

from __future__ import annotations

import math
from typing import Literal, Protocol, cast

from pydantic import field_validator

from agent_harness.contracts.dto import HarnessDTO


class ModelRequest(HarnessDTO):
    """进入模型路由前的稳定请求形状，不携带 provider SDK 对象。"""

    provider: str = "fake"
    prompt: str
    model: str | None = None
    estimated_input_tokens: int = 0
    max_output_tokens: int = 0
    timeout_seconds: int | None = None


class ModelDecision(HarnessDTO):
    """模型路由对调用、fallback 或策略介入的可追踪判断。"""

    action: str
    estimated_tokens: int
    max_tokens: int | None = None
    fallback_model: str | None = None
    reason: str | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None


class ModelResponse(HarnessDTO):
    """provider adapter 返回给 runtime 的统一结果。"""

    provider: str
    model: str
    output_text: str
    decision: ModelDecision
    token_usage: dict[str, int]
    latency_ms: int = 0
    cost_usd: float | None = None
    cost_status: Literal["reported", "estimated", "unavailable"] = "unavailable"

    @field_validator("token_usage", mode="before")
    @classmethod
    def validate_token_usage(cls, value: object) -> object:
        """拒绝负数、bool 与隐式字符串，避免无效 adapter 结果进入结算。"""

        if not isinstance(value, dict):
            return value
        token_usage = cast(dict[object, object], value)
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in token_usage.values()
        ):
            raise ValueError("model token usage must contain non-negative integers")
        return token_usage

    @field_validator("latency_ms", mode="before")
    @classmethod
    def validate_latency(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("model latency must be a non-negative integer")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def validate_cost(cls, value: object) -> object:
        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("model cost must be finite and non-negative")
        return value


class ModelProvider(Protocol):
    """所有模型 adapter 必须实现的最小 provider seam。"""

    provider_id: str

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        """执行一次模型调用，provider SDK 细节留在 adapter 后面。"""
        ...
