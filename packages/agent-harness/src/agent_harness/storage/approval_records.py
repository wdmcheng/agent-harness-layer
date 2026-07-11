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


class ApprovalResolutionQueueState(HarnessDTO):
    """service worker 可读取、但 public ApprovalRecord 不公开的状态。"""

    approval_id: str
    run_id: str
    tenant_id: str
    lease_id: str
    resolution_state: str
    operation_id: str
    request_id: str
    reviewer_id: str
    decision: str
    request_hash: str
    comment: str | None = None
    enqueue_state: str
    message_id: str | None = None
    workflow_owner_id: str | None = None
    workflow_id: str | None = None
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


def approval_resolution_queue_state(model: ApprovalModel) -> ApprovalResolutionQueueState:
    """校验并投影完整私有 service resolution 状态。"""

    required = {
        "lease_id": model.resolution_lease_id,
        "resolution_state": model.resolution_state,
        "operation_id": model.resolution_operation_id,
        "request_id": model.resolution_request_id,
        "reviewer_id": model.resolution_reviewer_id,
        "decision": model.resolution_decision,
        "request_hash": model.resolution_request_hash,
        "enqueue_state": model.resolution_enqueue_state,
    }
    missing = [name for name, value in required.items() if value is None or value == ""]
    if missing:
        raise RuntimeError(f"approval resolution state incomplete: {', '.join(missing)}")
    return ApprovalResolutionQueueState(
        approval_id=model.id,
        run_id=model.run_id,
        tenant_id=model.tenant_id,
        lease_id=str(model.resolution_lease_id),
        resolution_state=str(model.resolution_state),
        operation_id=str(model.resolution_operation_id),
        request_id=str(model.resolution_request_id),
        reviewer_id=str(model.resolution_reviewer_id),
        decision=str(model.resolution_decision),
        request_hash=str(model.resolution_request_hash),
        comment=model.resolution_comment,
        enqueue_state=str(model.resolution_enqueue_state),
        message_id=model.resolution_message_id,
        workflow_owner_id=model.resolution_workflow_owner_id,
        workflow_id=model.resolution_workflow_id,
        claimed_at=model.resolution_claimed_at,
    )
