"""追加式 audit log DTO 与 repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import AuditLogModel


class AuditLogCreate(HarnessDTO):
    """写入 audit_logs 的结构化审计输入。"""

    tenant_id: str
    actor_user_id: str | None = None
    action: str
    resource: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditLogRecord(AuditLogCreate):
    """已持久化的审计记录。"""

    id: str
    created_at: datetime | None = None


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


class AuditLogRepository:
    """AuditService 使用的追加式审计 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: AuditLogCreate) -> AuditLogRecord:
        """追加一条结构化审计记录；调用方负责先完成 secret redaction。"""

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
