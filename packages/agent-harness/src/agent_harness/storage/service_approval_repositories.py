"""Service approval queue与execution-owned私有状态repository。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.approval_records import (
    ApprovalResolutionQueueState,
    approval_resolution_queue_state,
)
from agent_harness.storage.models import ApprovalModel, ToolInvocationModel


class ServiceApprovalResolutionRepositoryMixin:
    """隔离service fingerprint、queue与DBOS owner条件更新。"""

    _session: AsyncSession

    async def _raise_resolution_conflict(
        self, approval_id: str, run_id: str, tenant_id: str
    ) -> None:
        """将审批所有权冲突转换为上层约定的领域错误。"""

        ...

    async def claim_service_resolution(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        reviewer_id: str,
        decision: str,
        request_hash: str,
        request_id: str,
        comment: str | None = None,
    ) -> ApprovalResolutionQueueState:
        """原子取得 pre-execution lease 并写入完整私有 fingerprint。"""

        lease_id = str(uuid4())
        operation_id = f"run:{run_id}:approval:{approval_id}:lease:{lease_id}"
        now = datetime.now(tz=UTC)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_lease_id.is_(None),
                )
                .values(
                    resolution_lease_id=lease_id,
                    resolution_state="claimed",
                    resolution_claimed_at=now,
                    resolution_operation_id=operation_id,
                    resolution_request_id=request_id,
                    resolution_reviewer_id=reviewer_id,
                    resolution_decision=decision,
                    resolution_request_hash=request_hash,
                    resolution_comment=comment,
                    resolution_enqueue_state="enqueue_pending",
                )
            ),
        )
        if result.rowcount != 1:
            await self._raise_resolution_conflict(approval_id, run_id, tenant_id)
        model = await self._session.get(ApprovalModel, approval_id)
        assert model is not None
        return approval_resolution_queue_state(model)

    async def get_resolution_queue_state(
        self, approval_id: str
    ) -> ApprovalResolutionQueueState | None:
        """读取审批的私有队列状态；未创建 resolution 操作时返回 ``None``。"""

        model = await self._session.get(ApprovalModel, approval_id)
        if model is None or model.resolution_operation_id is None:
            return None
        return approval_resolution_queue_state(model)

    async def has_tool_claim(self, approval_id: str) -> bool:
        """确认审批是否已生成工具调用，防止恢复逻辑重复入队或执行。"""

        result = await self._session.execute(
            select(ToolInvocationModel.id).where(ToolInvocationModel.approval_id == approval_id)
        )
        return result.first() is not None

    async def list_pending_resolution_enqueue(
        self,
    ) -> list[ApprovalResolutionQueueState]:
        """列出已取得审批 lease、尚未完成入队且没有工具调用的恢复候选项。

        查询结果必须额外排除已有 tool claim 的记录：队列投递与工具执行分属不同
        事务边界，不能仅凭 enqueue 状态推断外部副作用尚未发生。
        """

        result = await self._session.scalars(
            select(ApprovalModel).where(
                ApprovalModel.status == "waiting",
                ApprovalModel.resolution_state == "claimed",
                ApprovalModel.resolution_enqueue_state == "enqueue_pending",
            )
        )
        states: list[ApprovalResolutionQueueState] = []
        for model in result.all():
            if not await self.has_tool_claim(model.id):
                states.append(approval_resolution_queue_state(model))
        return states

    async def mark_resolution_queued(
        self,
        *,
        approval_id: str,
        lease_id: str,
        operation_id: str,
        message_id: str,
    ) -> ApprovalResolutionQueueState:
        """以 lease 和 operation CAS 标记消息已入队，拒绝旧 owner 覆盖新状态。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.resolution_lease_id == lease_id,
                    ApprovalModel.resolution_operation_id == operation_id,
                    ApprovalModel.resolution_state == "claimed",
                    ApprovalModel.resolution_enqueue_state.in_(["enqueue_pending", "queued"]),
                )
                .values(
                    resolution_enqueue_state="queued",
                    resolution_message_id=message_id,
                )
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"approval queue state conflict: {approval_id}")
        model = await self._session.get(ApprovalModel, approval_id)
        assert model is not None
        return approval_resolution_queue_state(model)

    async def claim_resolution_execution(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        run_id: str,
        lease_id: str,
        operation_id: str,
        request_id: str,
        message_id: str,
        workflow_owner_id: str,
        workflow_id: str,
    ) -> bool:
        """原子取得审批执行所有权，确保同一批准最多创建一次工具调用。

        所有 fingerprint、消息和 DBOS workflow owner 字段都参与条件更新，避免
        过期 worker、伪造消息或同一审批的并发消费者越过 waiting 边界。
        """

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_lease_id == lease_id,
                    ApprovalModel.resolution_operation_id == operation_id,
                    ApprovalModel.resolution_request_id == request_id,
                    ApprovalModel.resolution_message_id == message_id,
                    ApprovalModel.resolution_reviewer_id.is_not(None),
                    ApprovalModel.resolution_reviewer_id != "",
                    ApprovalModel.resolution_decision == "approve",
                    ApprovalModel.resolution_request_hash.is_not(None),
                    ApprovalModel.resolution_request_hash != "",
                    ApprovalModel.resolution_state == "claimed",
                    ApprovalModel.resolution_enqueue_state == "queued",
                    ~exists().where(ToolInvocationModel.approval_id == approval_id),
                )
                .values(
                    resolution_state="execution_owned",
                    resolution_workflow_owner_id=workflow_owner_id,
                    resolution_workflow_id=workflow_id,
                    resolution_claimed_at=datetime.now(tz=UTC),
                )
            ),
        )
        return result.rowcount == 1

    async def takeover_service_resolution(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        reviewer_id: str,
        decision: str,
        request_hash: str,
        request_id: str,
        expired_before: datetime,
        comment: str | None = None,
    ) -> ApprovalResolutionQueueState | None:
        """matching真实请求接管过期 execution owner，并建立新 operation。"""

        lease_id = str(uuid4())
        operation_id = f"run:{run_id}:approval:{approval_id}:lease:{lease_id}"
        now = datetime.now(tz=UTC)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_state == "execution_owned",
                    ApprovalModel.resolution_claimed_at <= expired_before,
                    ApprovalModel.resolution_reviewer_id == reviewer_id,
                    ApprovalModel.resolution_decision == decision,
                    ApprovalModel.resolution_request_hash == request_hash,
                    ~exists().where(ToolInvocationModel.approval_id == approval_id),
                )
                .values(
                    resolution_lease_id=lease_id,
                    resolution_state="claimed",
                    resolution_claimed_at=now,
                    resolution_operation_id=operation_id,
                    resolution_request_id=request_id,
                    resolution_reviewer_id=reviewer_id,
                    resolution_decision=decision,
                    resolution_request_hash=request_hash,
                    resolution_comment=comment,
                    resolution_enqueue_state="enqueue_pending",
                    resolution_message_id=None,
                    resolution_workflow_owner_id=None,
                    resolution_workflow_id=None,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        model = await self._session.get(ApprovalModel, approval_id)
        assert model is not None
        return approval_resolution_queue_state(model)
