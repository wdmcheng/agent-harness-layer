"""审批 repository 的公开记录与私有租约 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import ApprovalModel


class ApprovalCreate(HarnessDTO):
    """创建 waiting approval 所需的 run、动作和 resume 关联字段。"""

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
    """approval 状态机的公开 repository 记录。"""

    approval_id: str
    status: str
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None


class ApprovalResolutionLease(HarnessDTO):
    """私有 repository state；绝不能嵌入 public DTO。"""

    approval: ApprovalRecord
    lease_id: str
    state: str
    claimed_at: datetime | None = None


class ApprovalResolutionRepositoryConflict(RuntimeError):
    """条件 resolution update 已输给另一个 resolver。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def approval_record(model: ApprovalModel) -> ApprovalRecord:
    """把 ORM model 投影成不公开 private resolution 字段的 DTO。"""

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
