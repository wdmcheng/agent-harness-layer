"""追加式 audit log DTO 与 repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import AuditLogModel
from agent_harness.storage.run_trace_gate import (
    RunTraceScopeConflict,
    project_canonical_run_trace,
)


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
    record_scope: Literal["run", "non_run"]
    created_at: datetime | None = None


def _audit_log_record(model: AuditLogModel) -> AuditLogRecord:
    """将 ORM 审计行转换为 DTO，保留 scope 以区分 run 与非 run 证据。"""

    return AuditLogRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        actor_user_id=model.actor_user_id,
        action=model.action,
        resource=model.resource,
        payload=model.payload_json,
        record_scope=cast(Literal["run", "non_run"], model.record_scope),
        created_at=model.created_at,
    )


class AuditLogRepository:
    """AuditService 使用的追加式审计 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前 UoW session；审计追加与业务状态由调用方共同提交。"""

        self._session = session

    async def create(self, data: AuditLogCreate) -> AuditLogRecord:
        """追加一条结构化审计记录；调用方负责先完成 secret redaction。"""

        payload = dict(data.payload)
        raw_run_id = payload.get("run_id")
        run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
        if raw_run_id is not None and run_id is None:
            raise RunTraceScopeConflict
        record_scope: Literal["run", "non_run"] = "non_run"
        if run_id is not None:
            raw_trace_id = payload.get("trace_id")
            trace_id = raw_trace_id if isinstance(raw_trace_id, str) else None
            canonical = await project_canonical_run_trace(
                self._session,
                tenant_id=data.tenant_id,
                run_id=run_id,
                trace_id=trace_id,
            )
            payload["trace_id"] = canonical
            record_scope = "run"

        model = AuditLogModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            actor_user_id=data.actor_user_id,
            action=data.action,
            resource=data.resource,
            payload_json=payload,
            record_scope=record_scope,
        )
        self._session.add(model)
        await self._session.flush()
        return _audit_log_record(model)

    async def list_for_tenant(self, tenant_id: str) -> list[AuditLogRecord]:
        """按创建顺序列出租户审计记录，不允许无 tenant 范围的全局读取。"""

        result = await self._session.scalars(
            select(AuditLogModel)
            .where(AuditLogModel.tenant_id == tenant_id)
            .order_by(AuditLogModel.created_at.asc())
        )
        return [_audit_log_record(model) for model in result.all()]
