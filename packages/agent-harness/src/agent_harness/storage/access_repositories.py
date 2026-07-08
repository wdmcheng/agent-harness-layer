"""认证、策略、审批与审计 repository。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import (
    ApiKeyModel,
    ApprovalModel,
    AuditLogModel,
    PolicyRuleModel,
)


class ApiKeyCreate(HarnessDTO):
    tenant_id: str
    user_id: str
    name: str
    token_hash: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    disabled: bool = False


class ApiKeyRecord(ApiKeyCreate):
    id: str


class PolicyRuleCreate(HarnessDTO):
    tenant_id: str
    name: str
    action: str
    decision: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyRuleRecord(PolicyRuleCreate):
    id: str


class ApprovalCreate(HarnessDTO):
    tenant_id: str
    run_id: str
    agent_id: str
    action: str
    resource: str
    reason: str
    resume_token: str | None = None
    requested_by: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRecord(ApprovalCreate):
    approval_id: str
    status: str
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None


class AuditLogCreate(HarnessDTO):
    tenant_id: str
    actor_user_id: str | None = None
    action: str
    resource: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditLogRecord(AuditLogCreate):
    id: str
    created_at: datetime | None = None


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


def _approval_record(model: ApprovalModel) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=model.id,
        tenant_id=model.tenant_id,
        run_id=model.run_id,
        agent_id=model.agent_id,
        action=model.action,
        resource=model.resource,
        reason=model.reason,
        resume_token=model.resume_token,
        requested_by=model.requested_by,
        trace_id=model.trace_id,
        request_id=model.request_id,
        metadata=model.metadata_json,
        status=model.status,
        resolved_by=model.resolved_by,
        resolved_at=model.resolved_at,
        created_at=model.created_at,
    )


def _audit_log_record(model: AuditLogModel) -> AuditLogRecord:
    return AuditLogRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        actor_user_id=model.actor_user_id,
        action=model.action,
        resource=model.resource,
        payload=model.payload_json,
        created_at=model.created_at,
    )


class ApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ApiKeyCreate) -> ApiKeyRecord:
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
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: PolicyRuleCreate) -> PolicyRuleRecord:
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
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ApprovalCreate) -> ApprovalRecord:
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
        return _approval_record(model)

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        model = await self._session.get(ApprovalModel, approval_id)
        return None if model is None else _approval_record(model)

    async def list_by_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
    ) -> list[ApprovalRecord]:
        conditions = [ApprovalModel.run_id == run_id]
        if tenant_id is not None:
            conditions.append(ApprovalModel.tenant_id == tenant_id)
        result = await self._session.scalars(
            select(ApprovalModel).where(*conditions).order_by(ApprovalModel.created_at.asc())
        )
        return [_approval_record(model) for model in result.all()]

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
        return _approval_record(model)


class AuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: AuditLogCreate) -> AuditLogRecord:
        model = AuditLogModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            actor_user_id=data.actor_user_id,
            action=data.action,
            resource=data.resource,
            payload_json=data.payload,
        )
        self._session.add(model)
        await self._session.flush()
        return _audit_log_record(model)

    async def list_for_tenant(self, tenant_id: str) -> list[AuditLogRecord]:
        result = await self._session.scalars(
            select(AuditLogModel)
            .where(AuditLogModel.tenant_id == tenant_id)
            .order_by(AuditLogModel.created_at.asc())
        )
        return [_audit_log_record(model) for model in result.all()]
