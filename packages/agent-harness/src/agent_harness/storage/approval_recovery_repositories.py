"""审批 evidence 收口与崩溃恢复状态的 repository 操作。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.approval_records import (
    ApprovalRecord,
    ApprovalResolutionLease,
    ApprovalResolutionRepositoryConflict,
    approval_record,
)
from agent_harness.storage.models import AgentRunModel, ApprovalModel


def required_resolution_request_id(model: ApprovalModel) -> str:
    """恢复私有仲裁状态时拒绝猜测已经丢失的首次请求关联。"""

    if not model.resolution_request_id:
        raise RuntimeError(f"approval resolution request id missing: {model.id}")
    return model.resolution_request_id


class ApprovalRecoveryRepositoryMixin:
    """维护审批结果公开前的 evidence 完整性与恢复状态。"""

    _session: AsyncSession

    async def mark_approved_evidence_complete(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        lease_id: str,
    ) -> ApprovalRecord:
        """resolution 与 terminal 均确认后才公开 approved。"""

        run_status = await self._session.scalar(
            select(AgentRunModel.status).where(AgentRunModel.id == run_id)
        )
        if run_status not in {"completed", "failed"}:
            raise RuntimeError("approval terminal run result is unavailable")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_lease_id == lease_id,
                    ApprovalModel.resolution_state.in_(["completed", "failed", "recovery_pending"]),
                )
                .values(status="approved", resolution_state=run_status)
            ),
        )
        if result.rowcount != 1:
            await self._raise_resolution_conflict(approval_id, run_id, tenant_id)
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approval not found: {approval_id}")
        return approval_record(model)

    async def mark_needs_review(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        lease_id: str,
    ) -> ApprovalRecord:
        """副作用结果不确定时保持 public status waiting。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_lease_id == lease_id,
                )
                .values(resolution_state="needs_review")
            ),
        )
        if result.rowcount != 1:
            await self._raise_resolution_conflict(approval_id, run_id, tenant_id)
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approval not found: {approval_id}")
        return approval_record(model)

    async def mark_recovery_pending(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        lease_id: str,
    ) -> bool:
        """把已返回基础设施异常的 approve lease 标记为可安全补偿。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_lease_id == lease_id,
                    ApprovalModel.resolution_state.in_(
                        [
                            "claimed",
                            "execution_owned",
                            "recovery_pending",
                            "completed",
                            "failed",
                        ]
                    ),
                )
                .values(resolution_state="recovery_pending")
            ),
        )
        return result.rowcount == 1

    async def mark_denied_evidence_complete(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
    ) -> bool:
        """只在 denied terminal/resolution evidence 齐全后封存 private state。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.id == approval_id,
                    ApprovalModel.run_id == run_id,
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.status == "waiting",
                    ApprovalModel.resolution_state == "denied_pending",
                )
                .values(status="denied", resolution_state="denied")
            ),
        )
        return result.rowcount == 1

    async def get_resolution_state(self, approval_id: str) -> str | None:
        """读取不进入 public DTO 的 reconciliation 状态。"""

        model = await self._session.get(ApprovalModel, approval_id)
        return None if model is None else model.resolution_state

    async def get_resolution_request_id(self, approval_id: str) -> str | None:
        """读取审批仲裁事务冻结的首次请求关联。"""

        model = await self._session.get(ApprovalModel, approval_id)
        return None if model is None else model.resolution_request_id

    async def get_resolution(
        self,
        approval_id: str,
    ) -> ApprovalResolutionLease | None:
        """只为受控崩溃恢复读取 private lease state。"""

        model = await self._session.get(ApprovalModel, approval_id)
        if model is None or model.resolution_lease_id is None:
            return None
        return ApprovalResolutionLease(
            approval=approval_record(model),
            lease_id=model.resolution_lease_id,
            state=model.resolution_state or "claimed",
            resolution_request_id=required_resolution_request_id(model),
            claimed_at=model.resolution_claimed_at,
        )

    async def _raise_resolution_conflict(
        self,
        approval_id: str,
        run_id: str,
        tenant_id: str,
    ) -> None:
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None or model.run_id != run_id or model.tenant_id != tenant_id:
            raise LookupError(f"approval not found: {approval_id}")
        if model.status != "waiting":
            raise ApprovalResolutionRepositoryConflict(
                "approval.invalid_transition",
                f"approval is already {model.status}: {approval_id}",
            )
        raise ApprovalResolutionRepositoryConflict(
            "approval.resolution_in_progress",
            f"approval resolution is in progress: {approval_id}",
        )


__all__ = ["ApprovalRecoveryRepositoryMixin", "required_resolution_request_id"]
