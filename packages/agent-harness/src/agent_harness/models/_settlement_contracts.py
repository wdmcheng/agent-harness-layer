"""模型 settlement 的冻结状态、稳定错误与窄身份协议。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

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
        }
    )

    def __init__(
        self,
        code: str = "model.provider_failed",
        *,
        provider_called: bool = False,
        attempt_count: int = 0,
        latency_ms: int | None = None,
    ) -> None:
        """封闭 raw 异常，同时让运维入口按事实报告是否已发生副作用。"""

        if attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if provider_called != (attempt_count > 0):
            raise ValueError("provider_called and attempt_count must describe the same side effect")

        message = "model provider invocation failed" if code == self.code else code
        super().__init__(message)
        self.code = code
        self.provider_called = provider_called
        self.attempt_count = attempt_count
        self.latency_ms = latency_ms


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
