"""审批 repository 与历史认证/策略导入兼容门面。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.access_policy_repositories import ApiKeyCreate as ApiKeyCreate
from agent_harness.storage.access_policy_repositories import ApiKeyRecord as ApiKeyRecord
from agent_harness.storage.access_policy_repositories import (
    ApiKeyRepository as ApiKeyRepository,
)
from agent_harness.storage.access_policy_repositories import (
    PolicyRuleCreate as PolicyRuleCreate,
)
from agent_harness.storage.access_policy_repositories import (
    PolicyRuleRecord as PolicyRuleRecord,
)
from agent_harness.storage.access_policy_repositories import (
    PolicyRuleRepository as PolicyRuleRepository,
)
from agent_harness.storage.approval_records import (
    ApprovalCreate,
    ApprovalRecord,
    ApprovalResolutionLease,
    approval_record,
)
from agent_harness.storage.approval_records import (
    ApprovalResolutionRepositoryConflict as ApprovalResolutionRepositoryConflict,
)
from agent_harness.storage.approval_recovery_repositories import (
    ApprovalRecoveryRepositoryMixin,
    required_resolution_request_id,
)
from agent_harness.storage.models import ApprovalModel, ToolInvocationModel
from agent_harness.storage.run_trace_gate import require_canonical_run_trace
from agent_harness.storage.service_approval_repositories import (
    ServiceApprovalResolutionRepositoryMixin,
)


class ApprovalRepository(
    ApprovalRecoveryRepositoryMixin,
    ServiceApprovalResolutionRepositoryMixin,
):
    """ApprovalService 使用的 waiting/resolve 状态 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前 UoW，会话生命周期由 service 装配层而非仓储自行管理。"""

        self._session = session

    async def create(self, data: ApprovalCreate) -> ApprovalRecord:
        """创建初始 waiting approval，resolve 只能走 `resolve()`。"""

        trace_id = await require_canonical_run_trace(
            self._session,
            tenant_id=data.tenant_id,
            run_id=data.run_id,
            trace_id=data.trace_id,
        )

        model = ApprovalModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            run_id=data.run_id,
            agent_id=data.agent_id,
            action=data.action,
            resource=data.resource,
            reason=data.reason,
            status="waiting",
            resume_token=data.resume_token,
            requested_by=data.requested_by,
            trace_id=trace_id,
            request_id=data.request_id,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return approval_record(model)

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        """按 approval 主键读取稳定 DTO，供恢复与鉴权路径复核归属状态。"""

        model = await self._session.get(ApprovalModel, approval_id)
        return None if model is None else approval_record(model)

    async def list_by_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        for_update: bool = False,
    ) -> list[ApprovalRecord]:
        """按 run 顺序读取 approvals；传入 tenant_id 时同时执行租户过滤。"""

        conditions = [ApprovalModel.run_id == run_id]
        if tenant_id is not None:
            conditions.append(ApprovalModel.tenant_id == tenant_id)
        statement = (
            select(ApprovalModel).where(*conditions).order_by(ApprovalModel.created_at.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return [approval_record(model) for model in result.all()]

    async def resolve(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        status: str,
        resolved_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """把 waiting approval 转成最终状态，并保留 resolve 审计元数据。"""

        model = await self._session.get(ApprovalModel, approval_id)
        if model is None or model.run_id != run_id or model.tenant_id != tenant_id:
            raise LookupError(f"approval not found: {approval_id}")
        if model.status != "waiting":
            raise RuntimeError(f"approval is already {model.status}: {approval_id}")
        model.status = status
        model.resolved_by = resolved_by
        model.resolved_at = datetime.now(tz=UTC)
        if metadata:
            model.metadata_json = {**model.metadata_json, **metadata}
        await self._session.flush()
        return approval_record(model)

    async def claim_resolution(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        request_id: str,
    ) -> ApprovalResolutionLease:
        """原子取得 approve lease，并冻结首次 resolution 请求关联。"""

        lease_id = str(uuid4())
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
                    ApprovalModel.resolution_state.is_(None),
                )
                .values(
                    resolution_lease_id=lease_id,
                    resolution_state="claimed",
                    resolution_claimed_at=now,
                    resolution_request_id=request_id,
                )
            ),
        )
        if result.rowcount != 1:
            await self._raise_resolution_conflict(approval_id, run_id, tenant_id)
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approval not found: {approval_id}")
        return ApprovalResolutionLease(
            approval=approval_record(model),
            lease_id=lease_id,
            state="claimed",
            resolution_request_id=request_id,
            claimed_at=now,
        )

    async def takeover_expired_resolution(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        expired_before: datetime,
    ) -> ApprovalResolutionLease | None:
        """只接管已过期且尚无 tool claim 的 raw claimed lease，并换发 fencing id。"""

        lease_id = str(uuid4())
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
                    ApprovalModel.resolution_state == "claimed",
                    ApprovalModel.resolution_operation_id.is_(None),
                    ApprovalModel.resolution_claimed_at <= expired_before,
                    ~exists().where(ToolInvocationModel.approval_id == approval_id),
                )
                .values(
                    resolution_lease_id=lease_id,
                    resolution_claimed_at=now,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None:  # pragma: no cover - guarded by conditional update
            return None
        return ApprovalResolutionLease(
            approval=approval_record(model),
            lease_id=lease_id,
            state="claimed",
            resolution_request_id=required_resolution_request_id(model),
            claimed_at=now,
        )

    async def fence_resolution_lease(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        lease_id: str,
    ) -> bool:
        """claim 创建事务内续租并锁定 fencing id，失效 owner 不得创建 tool claim。"""

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
                .values(resolution_claimed_at=datetime.now(tz=UTC))
            ),
        )
        return result.rowcount == 1

    async def deny_waiting(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        resolved_by: str,
        request_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """原子 deny，并冻结首次 resolution 请求关联。"""

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
                    ApprovalModel.resolution_state.is_(None),
                )
                .values(
                    resolved_by=resolved_by,
                    resolved_at=now,
                    resolution_state="denied_pending",
                    resolution_finalized_at=now,
                    resolution_request_id=request_id,
                )
            ),
        )
        if result.rowcount != 1:
            await self._raise_resolution_conflict(approval_id, run_id, tenant_id)
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approval not found: {approval_id}")
        if metadata:
            model.metadata_json = {**model.metadata_json, **metadata}
            await self._session.flush()
        return approval_record(model)

    async def finalize_approved(
        self,
        *,
        approval_id: str,
        run_id: str,
        tenant_id: str,
        lease_id: str,
        resolved_by: str,
        result_state: str,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """持久化确定性结果，但在 ordered evidence 完成前保持公开 waiting。"""

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
                    ApprovalModel.resolution_lease_id == lease_id,
                    ApprovalModel.resolution_state.in_(
                        ["claimed", "execution_owned", "recovery_pending"]
                    ),
                )
                .values(
                    resolved_by=resolved_by,
                    resolved_at=now,
                    resolution_state=result_state,
                    resolution_finalized_at=now,
                )
            ),
        )
        if result.rowcount != 1:
            await self._raise_resolution_conflict(approval_id, run_id, tenant_id)
        model = await self._session.get(ApprovalModel, approval_id)
        if model is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approval not found: {approval_id}")
        if metadata:
            model.metadata_json = {**model.metadata_json, **metadata}
            await self._session.flush()
        return approval_record(model)
