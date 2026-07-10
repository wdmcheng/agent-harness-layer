"""认证、策略、审批与审计 repository。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.approval_records import (
    ApprovalCreate,
    ApprovalRecord,
    ApprovalResolutionLease,
    ApprovalResolutionRepositoryConflict,
    approval_record,
)
from agent_harness.storage.models import (
    ApiKeyModel,
    ApprovalModel,
    PolicyRuleModel,
    ToolInvocationModel,
)


class ApiKeyCreate(HarnessDTO):
    """创建 API key 记录时的脱敏输入。"""

    tenant_id: str
    user_id: str
    name: str
    token_hash: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    disabled: bool = False


class ApiKeyRecord(ApiKeyCreate):
    """已持久化的 API key 记录，只包含 token hash。"""

    id: str


class PolicyRuleCreate(HarnessDTO):
    """DB policy provider 的规则写入 DTO。"""

    tenant_id: str
    name: str
    action: str
    decision: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyRuleRecord(PolicyRuleCreate):
    """已持久化的策略规则。"""

    id: str


def _api_key_record(model: ApiKeyModel) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        name=model.name,
        token_hash=model.token_hash,
        roles=model.roles_json,
        permissions=model.permissions_json,
        disabled=model.disabled,
    )


def _policy_rule_record(model: PolicyRuleModel) -> PolicyRuleRecord:
    return PolicyRuleRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        name=model.name,
        action=model.action,
        decision=model.decision,
        payload=model.payload_json,
    )


class ApiKeyRepository:
    """API key verifier 使用的 token hash 查询 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ApiKeyCreate) -> ApiKeyRecord:
        """写入已 hash 的 API key；repository 不接收明文 token。"""

        model = ApiKeyModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            name=data.name,
            token_hash=data.token_hash,
            roles_json=data.roles,
            permissions_json=data.permissions,
            disabled=data.disabled,
        )
        self._session.add(model)
        await self._session.flush()
        return _api_key_record(model)

    async def get_by_hash(self, token_hash: str) -> ApiKeyRecord | None:
        result = await self._session.scalars(
            select(ApiKeyModel).where(ApiKeyModel.token_hash == token_hash)
        )
        model = result.first()
        return None if model is None else _api_key_record(model)


class PolicyRuleRepository:
    """DB-backed PolicyProvider 使用的规则 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: PolicyRuleCreate) -> PolicyRuleRecord:
        """写入 DB policy provider 可读取的单条规则。"""

        model = PolicyRuleModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            name=data.name,
            action=data.action,
            decision=data.decision,
            payload_json=data.payload,
        )
        self._session.add(model)
        await self._session.flush()
        return _policy_rule_record(model)

    async def list_for_tenant(self, tenant_id: str) -> list[PolicyRuleRecord]:
        result = await self._session.scalars(
            select(PolicyRuleModel).where(PolicyRuleModel.tenant_id == tenant_id)
        )
        return [_policy_rule_record(model) for model in result.all()]


class ApprovalRepository:
    """ApprovalService 使用的 waiting/resolve 状态 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ApprovalCreate) -> ApprovalRecord:
        """创建初始 waiting approval，resolve 只能走 `resolve()`。"""

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
            trace_id=data.trace_id,
            request_id=data.request_id,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return approval_record(model)

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        model = await self._session.get(ApprovalModel, approval_id)
        return None if model is None else approval_record(model)

    async def list_by_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
    ) -> list[ApprovalRecord]:
        """按 run 顺序读取 approvals；传入 tenant_id 时同时执行租户过滤。"""

        conditions = [ApprovalModel.run_id == run_id]
        if tenant_id is not None:
            conditions.append(ApprovalModel.tenant_id == tenant_id)
        result = await self._session.scalars(
            select(ApprovalModel).where(*conditions).order_by(ApprovalModel.created_at.asc())
        )
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
    ) -> ApprovalResolutionLease:
        """为 waiting approval 的 approve continuation 原子取得 lease。"""

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
                )
                .values(
                    resolution_lease_id=lease_id,
                    resolution_state="claimed",
                    resolution_claimed_at=now,
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
                    ApprovalModel.resolution_state.in_(["claimed", "recovery_pending"]),
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
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """仅在 approve lease 尚未赢得仲裁时原子 deny。"""

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
                    status="denied",
                    resolved_by=resolved_by,
                    resolved_at=now,
                    resolution_state="denied_pending",
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
        """只有确定性 terminal run result 已存在时才公开 approved。"""

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
                    ApprovalModel.resolution_state.in_(["claimed", "recovery_pending"]),
                )
                .values(
                    status="approved",
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
                    ApprovalModel.resolution_state.in_(["claimed", "recovery_pending"]),
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
                    ApprovalModel.status == "denied",
                    ApprovalModel.resolution_state == "denied_pending",
                )
                .values(resolution_state="denied")
            ),
        )
        return result.rowcount == 1

    async def get_resolution_state(self, approval_id: str) -> str | None:
        """读取不进入 public DTO 的 reconciliation 状态。"""

        model = await self._session.get(ApprovalModel, approval_id)
        return None if model is None else model.resolution_state

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
