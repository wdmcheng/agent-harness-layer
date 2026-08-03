"""Model 与 embedding 共用的 provider-neutral usage evidence。"""

from __future__ import annotations

import math
from hashlib import sha256
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.structured import (
    OutputSchemaIdentity,
    maximum_structured_validation_codes,
)

UsageKind = Literal["model", "embedding"]
CostStatus = Literal["reported", "estimated", "unavailable"]


class UsageInvocationReplayError(RuntimeError):
    """稳定调用 ID 已有 durable settlement，禁止重放 provider/cache 副作用。"""

    code = "usage.settlement_replay_blocked"

    def __init__(self, state: str) -> None:
        """保留 durable settlement 状态，帮助恢复调用方停止重复 provider 副作用。"""

        super().__init__(f"usage call already has durable settlement: {state}")
        self.state = state


def stable_usage_call_id(*, context: UsageEvidenceContext, operation_key: str) -> str:
    """从 durable run correlation 与调用槽位生成可跨进程重放的稳定 ID。"""

    if not operation_key:
        raise ValueError("usage operation key must not be empty")
    canonical = "\x1f".join(
        (
            "usage-v1",
            context.tenant_id,
            context.run_id,
            context.agent_id,
            context.request_id or "",
            context.trace_id,
            operation_key,
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class UsageEvidenceContext(HarnessDTO):
    """由 runtime composition 注入的关联上下文，不含 provider 原始对象。"""

    tenant_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    request_id: str | None = None


class StructuredUsageValidationIssue(HarnessDTO):
    """Structured usage 摘要允许持久化的唯一去敏验证问题。"""

    code: str = Field(min_length=1)
    path: str

    @model_validator(mode="after")
    def validate_issue(self) -> StructuredUsageValidationIssue:
        """Code 使用冻结词汇，path 使用有界 RFC 6901 instance pointer。"""

        if self.code not in maximum_structured_validation_codes():
            raise ValueError("structured validation code is unsupported")
        if len(self.path.encode("utf-8")) > 1024 or self.path and not self.path.startswith("/"):
            raise ValueError("structured validation path is not a bounded JSON pointer")
        for index, character in enumerate(self.path):
            if character == "~" and (
                index + 1 >= len(self.path) or self.path[index + 1] not in {"0", "1"}
            ):
                raise ValueError("structured validation path contains an invalid escape")
        return self


class StructuredUsageSummary(HarnessDTO):
    """Started/final usage decision 中的 structured exact 终态联合体。"""

    schema_version: Literal["structured-output-evidence-v1"]
    schema_identity: OutputSchemaIdentity
    status: Literal[
        "started", "valid", "invalid", "extra_fields", "repair_exhausted", "failed", "needs_review"
    ]
    repair_limit: int = Field(ge=0, le=2, strict=True)
    repair_count: int | None
    provider_request_limit: int = Field(ge=1, strict=True)
    provider_request_count: int | None
    replay_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_issues: list[StructuredUsageValidationIssue]
    error_code: str | None

    @field_validator("repair_count", "provider_request_count", mode="before")
    @classmethod
    def validate_nullable_count(cls, value: object) -> object:
        """Count 只接受 exact 非负整数或 null，禁止 bool/coercion。"""

        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("structured count must be a non-negative integer or null")
        return value

    @model_validator(mode="after")
    def validate_terminal_union(self) -> StructuredUsageSummary:
        """逐值锁定 started、确定终态和 needs-review 的 nullable 规则。"""

        if self.repair_count is not None and self.repair_count > self.repair_limit:
            raise ValueError("structured summary repair count exceeds limit")
        if (
            self.provider_request_count is not None
            and self.provider_request_count > self.provider_request_limit
        ):
            raise ValueError("structured summary provider request count exceeds limit")
        issue_keys = [(item.path, item.code) for item in self.validation_issues]
        if issue_keys != sorted(set(issue_keys)):
            raise ValueError("structured validation issues must be unique and sorted")
        if self.status == "started":
            if (
                self.repair_count != 0
                or self.provider_request_count != 0
                or self.replay_identity is not None
                or self.validation_issues
                or self.error_code is not None
            ):
                raise ValueError("structured started summary contains terminal facts")
            return self
        if self.replay_identity is None:
            raise ValueError("structured terminal summary requires replay identity")
        if self.status != "needs_review" and (
            self.repair_count is None or self.provider_request_count is None
        ):
            raise ValueError("determinate structured summary requires exact counts")
        request_count = self.provider_request_count
        repair_count = self.repair_count
        if self.status == "valid":
            if not request_count or self.validation_issues or self.error_code is not None:
                raise ValueError("valid structured summary facts mismatch")
        elif self.status in {"invalid", "extra_fields"}:
            expected_error = (
                "model.structured_invalid"
                if self.status == "invalid"
                else "model.structured_extra_fields"
            )
            if (
                self.repair_limit != 0
                or repair_count != 0
                or not request_count
                or not self.validation_issues
                or self.error_code != expected_error
            ):
                raise ValueError("terminal structured schema failure facts mismatch")
        elif self.status == "repair_exhausted":
            if (
                self.repair_limit < 1
                or repair_count != self.repair_limit
                or not request_count
                or not self.validation_issues
                or self.error_code != "model.structured_repair_exhausted"
            ):
                raise ValueError("structured repair exhaustion facts mismatch")
        elif self.status == "needs_review":
            if self.error_code != "model.provider_side_effect_unknown":
                raise ValueError("structured needs-review facts mismatch")
        elif self.error_code not in {
            "model.provider_failed",
            "model.provider_retry_exhausted",
            "model.invocation_cancelled",
            "model.input_too_large",
        }:
            raise ValueError("structured failed summary error code is unsupported")
        return self


class ModelUsageEvidence(HarnessDTO):
    """可持久化、可聚合且不携带 provider SDK 对象的调用证据。"""

    usage_kind: UsageKind
    tenant_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    cost_status: CostStatus
    latency_ms: int
    decision: dict[str, Any]
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    request_id: str | None = None
    trace_id: str = Field(min_length=1)

    @field_validator("input_tokens", "output_tokens", "latency_ms", mode="before")
    @classmethod
    def validate_non_negative_integer(cls, value: object) -> object:
        """拒绝 bool、字符串和会在预算聚合时改变语义的非整数。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("usage integer metrics must be non-negative integers")
        return value

    @field_validator("latency_ms")
    @classmethod
    def validate_required_latency(cls, value: int) -> int:
        """保留已由整数校验器验证的延迟值，声明该指标在 usage evidence 中不可缺失。"""

        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def validate_non_negative_finite_cost(cls, value: object) -> object:
        """只接受真实 number；bool/字符串不能被 Pydantic 隐式转换。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("cost_usd must be a number or null")
        if not math.isfinite(value) or value < 0:
            raise ValueError("cost_usd must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_cost_and_cache_semantics(self) -> ModelUsageEvidence:
        """校验成本状态、估算价格来源与 cache hit 的“未知而非零”用量语义。"""

        if self.cost_status == "unavailable":
            if self.cost_usd is not None:
                raise ValueError("unavailable cost requires cost_usd=null")
        elif self.cost_usd is None:
            raise ValueError("reported or estimated cost requires cost_usd")

        if self.cost_status == "estimated":
            for field_name in ("price_source_ref", "price_source_version"):
                value = self.decision.get(field_name)
                if not isinstance(value, str) or not value:
                    raise ValueError(
                        "estimated cost requires decision price source reference and version"
                    )

        if self.decision.get("cache_status") == "hit":
            if self.decision.get("provider_called") is not False:
                raise ValueError("cache hit requires provider_called=false")
            if (
                any(
                    value is not None
                    for value in (self.input_tokens, self.output_tokens, self.cost_usd)
                )
                or self.cost_status != "unavailable"
            ):
                raise ValueError("cache hit usage must preserve unavailable token and cost fields")
        return self

    def to_payload(self) -> dict[str, Any]:
        """nullable token/cost 是必需字段，序列化时不得因 null 被删除。"""

        return self.model_dump(mode="json")


def model_usage_evidence(
    *,
    provider: str,
    model: str,
    token_usage: dict[str, int],
    latency_ms: int,
    decision: dict[str, Any],
    context: UsageEvidenceContext,
) -> ModelUsageEvidence:
    """把 adapter 的有限 provider-neutral 结果归一化为统一 evidence。"""

    return ModelUsageEvidence(
        usage_kind="model",
        tenant_id=context.tenant_id,
        provider=provider,
        model=model,
        input_tokens=token_usage.get("input_tokens"),
        output_tokens=token_usage.get("output_tokens"),
        cost_usd=None,
        cost_status="unavailable",
        latency_ms=latency_ms,
        decision=decision,
        run_id=context.run_id,
        agent_id=context.agent_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )


def embedding_usage_evidence(
    *,
    provider: str,
    model: str,
    cache_hit: bool,
    latency_ms: int,
    context: UsageEvidenceContext,
    input_tokens: int | None = None,
) -> ModelUsageEvidence:
    """把 embedding provider/cache 结果映射到同一 usage DTO。"""

    return ModelUsageEvidence(
        usage_kind="embedding",
        tenant_id=context.tenant_id,
        provider=provider,
        model=model,
        input_tokens=None if cache_hit else input_tokens,
        output_tokens=None,
        cost_usd=None,
        cost_status="unavailable",
        latency_ms=latency_ms,
        decision={"cache_status": "hit" if cache_hit else "miss", "provider_called": not cache_hit},
        run_id=context.run_id,
        agent_id=context.agent_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )


__all__ = [
    "CostStatus",
    "ModelUsageEvidence",
    "StructuredUsageSummary",
    "StructuredUsageValidationIssue",
    "UsageEvidenceContext",
    "UsageInvocationReplayError",
    "UsageKind",
    "embedding_usage_evidence",
    "model_usage_evidence",
    "stable_usage_call_id",
]
