"""Approval 私有待补偿状态、幂等 evidence 与 denied 收口。"""

from __future__ import annotations

from typing import Any

from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime import InvalidRunTransition, RunOrchestrator, RunResult, RunStatus
from agent_harness.storage import ApprovalRecord, SQLAlchemyStorage
from agent_harness.storage.access_repositories import ApprovalResolutionLease


def approval_evidence(record: ApprovalRecord) -> dict[str, Any]:
    """返回 event/audit 共用且不含 private lease 的 approval 摘要。"""

    return {
        "approval_id": record.approval_id,
        "tenant_id": record.tenant_id,
        "run_id": record.run_id,
        "agent_id": record.agent_id,
        "action": record.action,
        "resource": record.resource,
        "reason": record.reason,
        "status": record.status,
        "requested_by": record.requested_by,
        "resolved_by": record.resolved_by,
        "trace_id": record.trace_id,
        "request_id": record.request_id,
    }


async def publish_resolution_evidence(
    event_bus: EventBus,
    *,
    actor: IdentityContext,
    record: ApprovalRecord,
    request_id: str | None,
) -> None:
    """用 approval 级稳定 id 发布唯一 public resolution event。"""

    await event_bus.publish(
        tenant_id=actor.tenant_id,
        run_id=record.run_id,
        agent_id=record.agent_id,
        user_id=actor.user_id,
        event_type=CanonicalEventType.APPROVAL_RESOLVED,
        payload=approval_evidence(record),
        request_id=request_id,
        trace_id=record.trace_id,
        event_id=f"approval-resolution:{record.approval_id}",
    )


async def mark_recovery_pending(
    storage: SQLAlchemyStorage,
    lease: ApprovalResolutionLease,
) -> None:
    """基础设施异常返回调用方前持久化可重试状态；不冒充 needs-review。"""

    async with storage.uow() as uow:
        changed = await uow.approvals.mark_recovery_pending(
            approval_id=lease.approval.approval_id,
            run_id=lease.approval.run_id,
            tenant_id=lease.approval.tenant_id,
            lease_id=lease.lease_id,
        )
        if changed:
            await uow.commit()


async def reconcile_denied(
    storage: SQLAlchemyStorage,
    event_bus: EventBus,
    orchestrator: RunOrchestrator,
    *,
    actor: IdentityContext,
    record: ApprovalRecord,
    request_id: str | None,
) -> RunResult:
    """补齐 denied terminal/resolution，重复执行仍只保留一份 evidence。"""

    run_result = await orchestrator.get_run(record.run_id, identity=actor)
    if run_result.status != RunStatus.FAILED:
        if run_result.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            raise RuntimeError(f"denied approval has incompatible terminal run: {record.run_id}")
        try:
            run_result = await orchestrator.fail_run(
                record.run_id,
                reason="approval denied",
                identity=actor,
            )
        except InvalidRunTransition:
            run_result = await orchestrator.get_run(record.run_id, identity=actor)
            if run_result.status != RunStatus.FAILED:
                raise
    await publish_resolution_evidence(
        event_bus,
        actor=actor,
        record=record,
        request_id=request_id,
    )
    async with storage.uow() as uow:
        await uow.approvals.mark_denied_evidence_complete(
            approval_id=record.approval_id,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
        )
        await uow.commit()
    return run_result
