"""Runtime checkpoint/resume 的公开契约。"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class ResumeToken(HarnessDTO):
    """checkpoint resume 使用的稳定 token DTO。"""

    value: str


class IdempotencyKey(HarnessDTO):
    """防重复提交的稳定 key DTO。"""

    value: str


class ApprovalWaitState(HarnessDTO):
    """HITL approval 可以持久化的等待状态形状。"""

    approval_id: str
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckpointStore(Protocol):
    """Runtime checkpoint 存取协议。

    当前 runtime 实现走 Repository/UoW；公开 Protocol 先固定业务边界，避免
    后续 DBOS/Temporal adapter 把 vendor handle 泄漏给 runtime caller。
    """

    async def create_checkpoint(
        self,
        *,
        tenant_id: str,
        run_id: str,
        sequence: int,
        state: dict[str, Any],
    ) -> ResumeToken:
        """创建 checkpoint 并返回 resume token。"""
        ...

    async def resolve_resume_token(self, token: ResumeToken | str) -> str | None:
        """返回 token 所属 run id；未找到时返回 None。"""
        ...
