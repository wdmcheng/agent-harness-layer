"""Delegation service 的稳定类型、协议与错误边界。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.delegation.models import (
    DelegationSummary,
)
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.runtime import RunStatus

if TYPE_CHECKING:
    from agent_harness.storage.shared_budget import OperationIdentity

DelegationMode = Literal["local", "service"]
_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}


class DelegationOrchestrator(Protocol):
    """委派服务依赖的最小 run 编排协议，具体参数形状由运行时公开 DTO 定义。"""

    async def start_run(self, **kwargs: Any) -> Any:
        """在当前事务已准备好时创建 child run；返回值由具体编排器保持兼容。"""

        ...

    async def submit_run(self, **kwargs: Any) -> Any:
        """提交 child run 到运行时或队列；幂等与交接状态由具体编排器耐久化。"""

        ...

    async def resume_run(self, resume_token: str, **kwargs: Any) -> Any:
        """使用已验证的恢复令牌继续 child run，拒绝服务层自行伪造恢复状态。"""

        ...


class DelegationPolicy(Protocol):
    """委派动作所需的策略检查协议；仅返回决策，不执行创建或恢复副作用。"""

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """根据稳定 actor、action、resource 和上下文返回允许、拒绝或审批要求。"""

        ...


class DelegationBudgetIdentityRuntime(Protocol):
    """委派共享预算身份的构造与重放校验能力，隔离具体账本运行时实现。"""

    def delegation_identity(
        self,
        *,
        tenant_id: str,
        canonical_request_bytes: bytes,
        parent_run_id: str,
        source_agent_id: str,
        target_agent_id: str,
        delegation_id: str,
        idempotency_key: str,
        tree_snapshot_id: str,
        snapshot: dict[str, Any],
        trusted_token_bound: int,
        trusted_cost_bound: Decimal | None,
    ) -> OperationIdentity:
        """从冻结树快照与规范请求字节构造首次委派的不可变预算身份。"""

        ...

    def delegation_replay_identity(
        self,
        *,
        tenant_id: str,
        canonical_request_bytes: bytes,
        parent_run_id: str,
        source_agent_id: str,
        target_agent_id: str,
        delegation_id: str,
        idempotency_key: str,
        persisted_identity: OperationIdentity,
    ) -> OperationIdentity:
        """用已持久化 identity 的冻结字段重建重放期望值，禁止读取当前漂移配置。"""

        ...


class DelegationError(RuntimeError):
    """只暴露合同允许的稳定错误码，不回显内部身份、余额或 provider evidence。"""

    def __init__(self, code: str) -> None:
        """以稳定错误码构造异常，不携带 parent/child、余额或 provider 诊断内容。"""

        super().__init__(code)
        self.code = code


class DelegationExecutionResult(HarnessDTO):
    """委派执行完成后的最小结果，关联父子 run 与可选公开摘要。"""

    delegation_id: str
    parent_run_id: str
    child_run_id: str
    status: str
    summary: DelegationSummary | None


TERMINAL_RUN_STATUSES = _TERMINAL

__all__ = [
    "TERMINAL_RUN_STATUSES",
    "DelegationError",
    "DelegationBudgetIdentityRuntime",
    "DelegationExecutionResult",
    "DelegationMode",
    "DelegationOrchestrator",
    "DelegationPolicy",
]
