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
    ) -> None: ...

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
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None or model.resolution_operation_id is None:
            return None
        return approval_resolution_queue_state(model)

    async def has_tool_claim(self, approval_id: str) -> bool:
        result = await self._session.execute(
            select(ToolInvocationModel.id).where(ToolInvocationModel.approval_id == approval_id)
        )
        return result.first() is not None

    async def list_pending_resolution_enqueue(
        self,
    ) -> list[ApprovalResolutionQueueState]:
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
