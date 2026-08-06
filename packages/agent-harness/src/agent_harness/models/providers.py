"""模型 provider 无关的请求、响应与决策 DTO。"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from pydantic import AliasChoices, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from agent_harness.config.schemas import ModelRouteRef
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.streaming import MAX_STREAM_COLLECTOR_UTF8_BYTES, bounded_utf8_size
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    OutputSchemaIdentity,
    StructuredOutputAttemptEvidence,
    StructuredOutputResult,
)

if TYPE_CHECKING:
    from agent_harness.models.router import ModelRoutePlan
    from agent_harness.models.tool_intent import ProviderToolIntentCandidate


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
    route_refs: tuple[ModelRouteRef, ...] | None = Field(default=None, max_length=8)

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


class StructuredModelAttemptEvidence(ModelAttemptEvidence):
    """保留 text attempt 基字段并增加 exact structured 判别详情。"""

    structured_output: StructuredOutputAttemptEvidence

    @model_validator(mode="after")
    def validate_structured_attempt(self) -> StructuredModelAttemptEvidence:
        """发送、验证、proof 与 cleanup 事实必须组成唯一局部联合体。"""

        detail = self.structured_output
        proof = detail.not_started_proof
        if self.side_effect_state == "not_started":
            if proof is None or (
                proof.kind == "client_prepare_not_started"
                and detail.cleanup_status != "not_applicable"
            ):
                raise ValueError("not-started attempt proof/cleanup facts mismatch")
            if proof.attempt != self.attempt:
                raise ValueError("not-started proof/global attempt mismatch")
            if (
                self.completion_observed not in {False, None}
                or detail.validation_codes is not None
                or any(
                    value not in {0, None}
                    for value in (
                        self.input_tokens,
                        self.output_tokens,
                        self.cost_usd,
                        self.budget_charge_tokens,
                        self.budget_charge_cost_usd,
                    )
                )
            ):
                raise ValueError("not-started structured attempt contains send facts")
        elif proof is not None or detail.cleanup_status == "not_applicable":
            raise ValueError("sent or unknown attempt cannot carry a not-started proof")
        if detail.validation_codes is not None:
            if (
                self.side_effect_state != "started"
                or self.outcome != "completed"
                or self.completion_observed is not True
                or self.input_tokens is None
                or self.output_tokens is None
            ):
                raise ValueError("validated structured attempt requires completed metered output")
        return self


class StructuredProviderCandidate(HarnessDTO):
    """Adapter 单次 structured request 返回的唯一候选与计量真相源。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["structured-provider-candidate-v1"] = "structured-provider-candidate-v1"
    schema_identity: OutputSchemaIdentity
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    candidate: str | dict[str, Any]
    attempts: list[ModelAttemptEvidence]

    @field_validator("candidate", mode="before")
    @classmethod
    def validate_candidate_shape(cls, value: object) -> object:
        """只接受原始 JSON 字符串或普通 JSON object，不字符串化 SDK 对象。"""

        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            raise ValueError("structured candidate must be a JSON string or object")
        # canonical serializer 在核心模块执行递归 JSON-compatible 检查。
        from agent_harness.models.structured import canonical_structured_json

        candidate = cast(dict[str, Any], value)
        canonical_structured_json(candidate)
        return candidate

    @model_validator(mode="after")
    def validate_single_attempt(self) -> StructuredProviderCandidate:
        """每个 adapter send 只能贡献一个从 1 开始的 local attempt。"""

        if len(self.attempts) != 1 or self.attempts[0].attempt != 1:
            raise ValueError("structured candidate requires exactly one local attempt")
        attempt = self.attempts[0]
        if (
            attempt.side_effect_state != "started"
            or attempt.outcome != "completed"
            or attempt.completion_observed is not True
            or attempt.error_code is not None
        ):
            raise ValueError("structured candidate requires one completed send attempt")
        return self

    @staticmethod
    def validated_snapshot(value: object) -> StructuredProviderCandidate | None:
        """重建核心candidate快照；duck object、子类或后置篡改统一拒绝。"""

        if type(value) is not StructuredProviderCandidate:
            return None
        try:
            payload = StructuredProviderCandidate.model_dump(value, mode="python")
            snapshot = StructuredProviderCandidate.model_validate(payload)
        except (AttributeError, TypeError, ValueError):
            return None
        return snapshot.model_copy(deep=True)


class StructuredProviderPrepareError(RuntimeError):
    """Prepare 尚未返回 handle 且已证明未 send 的 provider-neutral 错误。"""

    def __init__(self, *, retryable: object) -> None:
        if not isinstance(retryable, bool):
            raise TypeError("structured provider prepare retryable must be a boolean")
        super().__init__("structured provider prepare failed")
        self.retryable = retryable

    def validated_retryable(self) -> bool | None:
        """读取精确核心错误的布尔快照；缺失、错型或子类一律不可重试。"""

        if type(self) is not StructuredProviderPrepareError:
            return None
        try:
            raw_retryable = cast(object, object.__getattribute__(self, "retryable"))
        except AttributeError:
            return None
        return raw_retryable if type(raw_retryable) is bool else None


class StructuredProviderCallError(RuntimeError):
    """Send 已开始后的封闭 provider 失败，不携带 raw SDK 异常。"""

    def __init__(self, *, code: str, attempts: list[ModelAttemptEvidence]) -> None:
        if code not in {"model.provider_failed", "model.provider_side_effect_unknown"}:
            raise ValueError("structured provider call error code is unsupported")
        if len(attempts) != 1 or attempts[0].attempt != 1:
            raise ValueError("structured provider call error requires one local attempt")
        attempt = attempts[0]
        self._validate_attempt(code=code, attempt=attempt)
        super().__init__(code)
        self.code = code
        # Adapter持有的原列表和DTO都不再与错误对象共享；执行器仍会在消费前
        # 独立重验，防止恶意实现直接替换公开属性绕过构造期快照。
        self.attempts: tuple[ModelAttemptEvidence, ...] = (attempt.model_copy(deep=True),)

    @staticmethod
    def _validate_attempt(*, code: str, attempt: ModelAttemptEvidence) -> None:
        """验证稳定错误码与唯一attempt组成的封闭联合体。"""

        if code not in {"model.provider_failed", "model.provider_side_effect_unknown"}:
            raise ValueError("structured provider call error code is unsupported")
        if code == "model.provider_failed" and (
            attempt.side_effect_state != "started"
            or attempt.outcome != "failed"
            or attempt.completion_observed is not True
            or attempt.error_code != code
        ):
            raise ValueError("structured provider failure requires one definite failed attempt")
        if code == "model.provider_side_effect_unknown" and (
            attempt.side_effect_state != "unknown"
            or attempt.outcome != "unknown"
            or attempt.completion_observed is True
            or attempt.error_code != code
        ):
            raise ValueError("unknown structured call error requires one unresolved attempt")

    def validated_attempt(self) -> tuple[str, ModelAttemptEvidence] | None:
        """返回错误码与attempt的重验证快照；任何后置篡改都按unknown处理。"""

        if type(self) is not StructuredProviderCallError:
            return None
        try:
            raw_code = cast(object, object.__getattribute__(self, "code"))
            raw_attempts = cast(object, object.__getattribute__(self, "attempts"))
        except AttributeError:
            return None
        if type(raw_code) is not str:
            return None
        if type(raw_attempts) is not tuple:
            return None
        checked_attempts = cast(tuple[object, ...], raw_attempts)
        if len(checked_attempts) != 1:
            return None
        raw_attempt = checked_attempts[0]
        if type(raw_attempt) is not ModelAttemptEvidence:
            return None
        try:
            payload = ModelAttemptEvidence.model_dump(raw_attempt, mode="python")
            snapshot = ModelAttemptEvidence.model_validate(payload)
            StructuredProviderCallError._validate_attempt(code=raw_code, attempt=snapshot)
        except (AttributeError, TypeError, ValueError):
            return None
        return raw_code, snapshot.model_copy(deep=True)


def _empty_attempts() -> list[ModelAttemptEvidence | StructuredModelAttemptEvidence]:
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
    attempts: list[StructuredModelAttemptEvidence | ModelAttemptEvidence] = Field(
        default_factory=_empty_attempts
    )
    structured_output: StructuredOutputResult | None = None

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

    def to_payload(self) -> dict[str, Any]:
        """Structured response 保留 attempt 嵌套联合体中的显式 null。"""

        payload = super().to_payload()
        if self.structured_output is not None:
            payload["structured_output"] = self.structured_output.model_dump(mode="json")
            payload["attempts"] = [
                item.model_dump(mode="json", exclude_none=False)
                if isinstance(item, StructuredModelAttemptEvidence)
                else item.to_payload()
                for item in self.attempts
            ]
        return payload


class ModelStreamDelta(HarnessDTO):
    """供应商中立的追加文本片段；稳定 ordinal 由 invocation 分配。"""

    text: str
    _utf8_bytes: int = PrivateAttr(default=0)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """空片段没有可观察意义，必须在 adapter 边界直接丢弃。"""

        if not value:
            raise ValueError("model stream delta text must not be empty")
        return value

    @model_validator(mode="after")
    def cache_bounded_utf8_size(self) -> ModelStreamDelta:
        """在 DTO 进入 adapter/invocation collector 前完成无大块复制的硬上限检查。"""

        size = bounded_utf8_size(self.text, max_bytes=MAX_STREAM_COLLECTOR_UTF8_BYTES)
        if size is None:
            raise ValueError("model stream delta exceeds the fixed collector bound")
        self._utf8_bytes = size
        return self

    @property
    def utf8_bytes(self) -> int:
        """返回校验时缓存的字节数，不在 invocation 中再次编码整段文本。"""

        return self._utf8_bytes


class ModelStreamUsage(HarnessDTO):
    """流关闭时已观察到的 provider-neutral 用量，不携带 SDK 对象。"""

    finality: Literal["partial", "complete"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_status: Literal["reported", "estimated", "unavailable"] = "unavailable"
    latency_ms: int

    @field_validator("input_tokens", "output_tokens", "latency_ms", mode="before")
    @classmethod
    def validate_integer(cls, value: object) -> object:
        """计量整数拒绝 bool、字符串与负数，未知必须保持 null。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("model stream usage integers must be non-negative integers or null")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def validate_cost(cls, value: object) -> object:
        """成本只接受有限非负 number；未知成本使用 null。"""

        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("model stream usage cost must be finite and non-negative or null")
        return value

    @model_validator(mode="after")
    def validate_cost_shape(self) -> ModelStreamUsage:
        """null 成本与 unavailable 一一对应，避免把未知误写成已报告零成本。"""

        if (self.cost_usd is None) != (self.cost_status == "unavailable"):
            raise ValueError("model stream usage cost and status are inconsistent")
        return self


class ModelStreamCloseResult(HarnessDTO):
    """本地关闭后对远端副作用与已观察用量的最小事实分类。"""

    state: Literal["not_started", "stopped", "unknown"]
    usage: ModelStreamUsage | None = None

    @model_validator(mode="after")
    def validate_state_usage(self) -> ModelStreamCloseResult:
        """未开始禁止用量，unknown 禁止把完整用量冒充停止证明。"""

        if self.state == "not_started" and self.usage is not None:
            raise ValueError("not_started stream close result cannot contain usage")
        if self.state == "unknown" and self.usage is not None and self.usage.finality == "complete":
            raise ValueError("unknown stream close result cannot contain complete usage")
        return self


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


@runtime_checkable
class PreparedModelToolIntentCall(Protocol):
    """已取得 permit/client、但未发送的 provider-neutral tool-intent 调用。"""

    async def send_tool_intent(self) -> ModelResponse | ProviderToolIntentCandidate:
        """发送恰好一次请求；不得执行或注册任何工具 callback。"""
        ...

    async def aclose(self) -> None:
        """释放本次调用资源；不得在清理时发送请求。"""
        ...


@runtime_checkable
class ModelToolIntentProvider(Protocol):
    """与 text/stream/structured 正交的工具意图观察能力。"""

    provider_id: str
    tool_intent_observation_supported: bool

    async def prepare_tool_intent(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        tool_catalog_json: bytes,
    ) -> PreparedModelToolIntentCall:
        """只映射核心冻结目录，不得读取 Registry 或 executable handler。"""
        ...


@runtime_checkable
class PreparedStructuredModelCall(Protocol):
    """已 prepare、未发送的单次 structured provider handle。"""

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """恰好执行一次 provider request，并返回唯一 local attempt。"""
        ...

    async def aclose(self) -> None:
        """释放本次 handle；不得发送、retry 或 repair。"""
        ...


@runtime_checkable
class ModelStructuredProvider(Protocol):
    """与 text/stream 正交的 provider-neutral structured 能力。"""

    provider_id: str

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> PreparedStructuredModelCall:
        """取得 fresh handle，但不得产生 provider request。"""
        ...


@runtime_checkable
class PreparedModelStreamCall(Protocol):
    """已 prepare 但保持惰性的文本流；首次迭代才允许 provider 副作用。"""

    def __aiter__(self) -> AsyncIterator[ModelStreamDelta]:
        """按供应商观察顺序返回追加文本，不公开 SDK event 或 cursor。"""
        ...

    async def result(self) -> ModelResponse:
        """流自然耗尽后返回唯一、已校验的最终结果。"""
        ...

    async def aclose(self) -> ModelStreamCloseResult:
        """确定性清理本地资源，并按可证明事实返回关闭分类。"""
        ...


@runtime_checkable
class ModelStreamingProvider(Protocol):
    """与一次性 complete 正交的供应商中立文本流能力。"""

    provider_id: str

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelStreamCall:
        """取得 permit/client lease，但不得开始网络请求或 SDK 迭代。"""
        ...
