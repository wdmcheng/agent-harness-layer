"""模型 settlement 的冻结状态、稳定错误与窄身份协议。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelResponse
from agent_harness.models.router import ModelRouterConfig
from agent_harness.models.usage import ModelUsageEvidence
from agent_harness.storage.evidence_repositories import UsageSettlementClaim
from agent_harness.storage.shared_budget import BudgetOperationOwnership, OperationIdentity


@dataclass(frozen=True)
class SettlementStart:
    """usage claim 启动后的耐久状态快照，决定调用方能否安全触发 provider 副作用。"""

    usage: UsageSettlementClaim
    ownership: BudgetOperationOwnership | None
    safe_to_start: bool = False
    started_evidence: ModelUsageEvidence | None = None


@dataclass(frozen=True)
class RouteAttemptNotStartedFacts:
    """可信未开始分类器交给 proof canonicalizer 的封闭事实。"""

    not_started_reason: Literal["client_not_started", "trusted_business_not_started"]
    side_effect_state: Literal["not_started", "started"]
    request_sent: bool
    http_response_observed: bool
    http_status: int | None
    response_identity_observed: bool
    usage_observed: bool
    text_observed: bool
    delta_observed: bool
    completion_observed: bool | None
    endpoint_policy_digest: str
    classifier_ref: str | None
    classifier_version: str | None


class ModelRouteChainExhaustedCause(HarnessDTO):
    """一个冻结 ordinal 的封闭耗尽原因。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    ordinal: int = Field(ge=1, le=8, strict=True)
    cause: Literal[
        "capability",
        "catalog",
        "input_bound",
        "hard_budget",
        "soft_budget",
        "balance",
        "not_started_failure",
    ]


class ModelRouteChainExhaustedDetail(HarnessDTO):
    """`model.route_chain_exhausted` 的 exact、去敏错误明细。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["model-route-chain-exhausted-v1"] = "model-route-chain-exhausted-v1"
    chain_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    causes: tuple[ModelRouteChainExhaustedCause, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_ordinals(self) -> ModelRouteChainExhaustedDetail:
        """原因必须覆盖从 1 开始的连续冻结候选，禁止遗漏或重排。"""

        if [item.ordinal for item in self.causes] != list(range(1, len(self.causes) + 1)):
            raise ValueError("route-chain exhausted causes must be continuous")
        return self


class ModelProviderInvocationError(RuntimeError):
    """provider 原异常已封闭，只暴露稳定错误码与安全副作用摘要。"""

    code = "model.provider_failed"
    stable_codes = frozenset(
        {
            "model.provider_failed",
            "model.provider_retry_exhausted",
            "model.provider_side_effect_unknown",
            "model.invocation_cancelled",
            "model.bulkhead_saturated",
            "model.route_chain_exhausted",
            "model.policy_denied",
            "model.structured_invalid",
            "model.structured_extra_fields",
            "model.structured_repair_exhausted",
            "model.structured_policy_invalid",
            "model.structured_schema_unknown",
            "model.structured_schema_conflict",
            "model.structured_route_not_allowed",
            "model.structured_capability_unsupported",
            "model.structured_replay_conflict",
            "model.input_too_large",
            "budget.reservation_rejected",
        }
    )

    def __init__(
        self,
        code: str = "model.provider_failed",
        *,
        provider_called: bool = False,
        attempt_count: int = 0,
        latency_ms: int | None = None,
        failure_domain: Literal["provider", "runtime"] = "provider",
        detail: ModelRouteChainExhaustedDetail | None = None,
    ) -> None:
        """封闭 raw 异常，并区分 provider 故障与本地运行时失败。"""

        if isinstance(attempt_count, bool) or attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if (
            code not in {"model.route_chain_exhausted", "model.provider_side_effect_unknown"}
            and provider_called
            and attempt_count == 0
        ):
            raise ValueError("provider_called requires at least one durable attempt")
        if failure_domain not in {"provider", "runtime"}:
            raise ValueError("failure_domain must be provider or runtime")
        if code != "model.route_chain_exhausted" and detail is not None:
            raise ValueError("only route-chain exhaustion may carry detail")

        message = "model provider invocation failed" if code == self.code else code
        super().__init__(message)
        self.code = code
        self.provider_called = provider_called
        self.attempt_count = attempt_count
        self.latency_ms = latency_ms
        self.failure_domain: Literal["provider", "runtime"] = failure_domain
        self.detail = detail


@dataclass(frozen=True)
class ValidatedSettlementResult:
    """完整校验后的耐久模型结果；只有该形状可越过 final 发布边界。"""

    evidence: ModelUsageEvidence
    outcome: str
    response: ModelResponse | None
    failure: ModelProviderInvocationError | None


class DurableMarkStateUnknown(asyncio.CancelledError):
    """取消发生在 durable mark 事务内，提交结果不得按未开始猜测。"""


class IdentityRuntime(Protocol):
    """模型结算所需的共享预算身份构造能力，隔离具体运行时实现。"""

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """用冻结的账本和路由事实构造可重放的不可变预算身份。"""

        ...

    def model_router_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        base: ModelRouterConfig,
    ) -> ModelRouterConfig:
        """从指定快照恢复 agent 的模型路由配置，避免新调用读取漂移中的当前配置。"""

        ...
