"""模型 provider 无关的请求、响应与决策 DTO。"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

from pydantic import AliasChoices, ConfigDict, Field, field_validator

from agent_harness.contracts.dto import HarnessDTO

if TYPE_CHECKING:
    from agent_harness.models.router import ModelRoutePlan


class ModelRequest(HarnessDTO):
    """进入模型路由前的稳定请求形状，不携带 provider SDK 对象。"""

    deployment_id: str | None = None
    provider: str | None = None
    prompt: str
    model: str | None = None
    capability: str = "text_completion"
    estimated_input_tokens: int = 0
    max_output_tokens: int = 0
    timeout_seconds: int | None = None

    @field_validator("estimated_input_tokens", "max_output_tokens", mode="before")
    @classmethod
    def validate_token_bound(cls, value: object) -> object:
        """拒绝 bool、负数和非整数请求预算，避免路由估算出现类型漂移。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("model token bounds must be non-negative integers")
        return value


class ModelDecision(HarnessDTO):
    """模型路由对调用、fallback 或策略介入的可追踪判断。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
    )

    action: str
    estimated_tokens: int
    max_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None
    max_cost_usd: Decimal | None = None
    fallback_model: str | None = None
    reason: str | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None


class ModelAttemptEvidence(HarnessDTO):
    """单次 provider attempt 的脱敏结果；不承载 raw body/header/exception。"""

    attempt: int
    side_effect_state: Literal["not_started", "started", "unknown"]
    outcome: Literal["completed", "failed", "retryable_status", "cancelled", "unknown"]
    completion_observed: bool | None = None
    http_status: int | None = Field(
        default=None,
        validation_alias=AliasChoices("http_status", "status_code"),
    )
    retry_after_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_status: Literal["reported", "estimated", "unavailable"] = "unavailable"
    budget_charge_tokens: int | None = None
    budget_charge_cost_usd: float | None = None
    latency_ms: int
    error_code: str | None = None

    @field_validator(
        "attempt",
        "retry_after_ms",
        "input_tokens",
        "output_tokens",
        "budget_charge_tokens",
        "latency_ms",
        mode="before",
    )
    @classmethod
    def validate_attempt_integer(cls, value: object) -> object:
        """attempt ordinal 必须为正，其余整数证据必须为非负且不能接受 bool。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("attempt integer evidence must be a non-negative integer")
        return value

    @field_validator("attempt")
    @classmethod
    def validate_attempt_ordinal(cls, value: int) -> int:
        """attempt ordinal 从 1 开始，避免零基编号进入公开证据。"""

        if value < 1:
            raise ValueError("attempt ordinal must start at one")
        return value

    @field_validator("http_status", mode="before")
    @classmethod
    def validate_http_status(cls, value: object) -> object:
        """只接受 HTTP 协议范围内的显式整数状态码。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
            raise ValueError("http_status must be an integer between 100 and 599")
        return value

    @field_validator("completion_observed", mode="before")
    @classmethod
    def validate_completion_observed(cls, value: object) -> object:
        """完成观察只接受真实 boolean 或 null，禁止 0/1 被 Pydantic 隐式转换。"""

        if value is not None and not isinstance(value, bool):
            raise ValueError("completion_observed must be a boolean or null")
        return value

    @field_validator("error_code", mode="before")
    @classmethod
    def validate_error_code(cls, value: object) -> object:
        """错误身份只能是非空稳定字符串或 null，不接收任意对象的字符串化结果。"""

        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("error_code must be a non-empty string or null")
        return value

    @field_validator("cost_usd", "budget_charge_cost_usd", mode="before")
    @classmethod
    def validate_attempt_cost(cls, value: object) -> object:
        """attempt 成本只能是有限非负 number；未知必须保持 null。"""

        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("attempt cost must be finite and non-negative")
        return value

    def to_payload(self) -> dict[str, object]:
        """5.29 规定的 nullable 字段也必须显式序列化，不能因 null 被删除。"""

        return self.model_dump(mode="json")


def _empty_attempts() -> list[ModelAttemptEvidence]:
    """为每个 provider response 创建独立 attempt 证据列表。"""

    return []


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
    attempts: list[ModelAttemptEvidence] = Field(default_factory=_empty_attempts)

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
        """验证 provider 返回的延迟为非负整数，禁止 SDK 的隐式字符串数值。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("model latency must be a non-negative integer")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def validate_cost(cls, value: object) -> object:
        """验证可选成本为有限非负数；``None`` 仅由 unavailable 语义解释。"""

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

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """执行一次模型调用，provider SDK 细节留在 adapter 后面。"""
        ...


@runtime_checkable
class ModelProviderLifecycle(Protocol):
    """描述持有进程级资源的 provider 生命周期；无资源 doubles 不需实现样板。"""

    async def aclose(self) -> None:
        """幂等释放 provider 持有的进程级 client。"""
        ...


class PreparedModelCall(Protocol):
    """已取得 process-local permit/client、但尚未发送网络请求的调用。"""

    async def send(self) -> ModelResponse:
        """只在 runtime 已持久化 durable started mark 后发送。"""
        ...

    async def aclose(self) -> None:
        """释放本次调用 permit；client 由进程级 factory 统一管理。"""
        ...
