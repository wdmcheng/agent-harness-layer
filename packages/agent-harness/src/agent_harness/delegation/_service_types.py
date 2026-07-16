"""Delegation service 的稳定类型、协议与错误边界。"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.delegation.models import (
    DelegationSummary,
)
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.runtime import RunStatus

DelegationMode = Literal["local", "service"]
_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}


class DelegationOrchestrator(Protocol):
    async def start_run(self, **kwargs: Any) -> Any: ...

    async def submit_run(self, **kwargs: Any) -> Any: ...

    async def resume_run(self, resume_token: str, **kwargs: Any) -> Any: ...


class DelegationPolicy(Protocol):
    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation: ...


class DelegationError(RuntimeError):
    """只暴露合同允许的稳定错误码，不回显内部身份、余额或 provider evidence。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DelegationExecutionResult(HarnessDTO):
    delegation_id: str
    parent_run_id: str
    child_run_id: str
    status: str
    summary: DelegationSummary | None


TERMINAL_RUN_STATUSES = _TERMINAL

__all__ = [
    "TERMINAL_RUN_STATUSES",
    "DelegationError",
    "DelegationExecutionResult",
    "DelegationMode",
    "DelegationOrchestrator",
    "DelegationPolicy",
]
