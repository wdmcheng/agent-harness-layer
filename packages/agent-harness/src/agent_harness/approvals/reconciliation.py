"""Approval 私有待补偿状态、幂等 evidence 与 denied 收口。"""

from __future__ import annotations

from typing import Any, cast

from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.events.sinks.base import EventSinkReplayConflict
from agent_harness.identity import IdentityContext
from agent_harness.runtime import InvalidRunTransition, RunOrchestrator, RunResult, RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import ApprovalRecord, SQLAlchemyStorage
from agent_harness.storage.access_repositories import ApprovalResolutionLease
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


def approval_evidence(record: ApprovalRecord) -> dict[str, Any]:
    """返回 event/audit 共用且不含 private lease 的 approval 摘要。"""

    evidence: dict[str, Any] = {
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
    continuation = record.metadata.get("continuation")
    if isinstance(continuation, dict):
        continuation_map = cast(dict[str, object], continuation)
    else:
        continuation_map = {}
    if continuation_map.get("kind") == "model_tool_loop":
        loop_id = continuation_map.get("loop_id")
        turn_ordinal = continuation_map.get("turn_ordinal")
        tool_call_id = continuation_map.get("tool_call_id")
        catalog_digest = continuation_map.get("catalog_digest")
        if (
            isinstance(loop_id, str)
            and loop_id
            and type(turn_ordinal) is int
            and turn_ordinal >= 1
            and isinstance(tool_call_id, str)
            and tool_call_id
            and isinstance(catalog_digest, str)
            and catalog_digest
        ):
            evidence["correlation"] = {
                "loop_id": loop_id,
                "turn_ordinal": turn_ordinal,
                "tool_call_id": tool_call_id,
                "catalog_digest": catalog_digest,
            }
    return evidence


def resolved_approval_record(record: ApprovalRecord, *, status: str) -> ApprovalRecord:
    """构造只用于 resolution evidence 的终态视图，不提前公开 repository 状态。"""

    if status not in {"approved", "denied"}:
        raise ValueError("approval resolution status must be approved or denied")
    return record.model_copy(update={"status": status})


def approval_evidence_group_id(approval_id: str) -> str:
    """返回 resolution 与 terminal 共用的稳定 ordered group id。"""

    return f"approval:{approval_id}:resolution"


async def stage_approval_evidence_group(
    uow: SQLAlchemyUnitOfWork,
    *,
    record: ApprovalRecord,
    resolution_status: str,
    run_status: RunStatus | None,
) -> None:
    """在公开状态前原子预约并写入 resolution -> terminal 两项。"""

    group_id = approval_evidence_group_id(record.approval_id)
    result = {
        "approval_id": record.approval_id,
        "resolution_status": resolution_status,
        "run_status": run_status.value if run_status is not None else "pending",
    }
    existing = await uow.evidence_outbox.ordered_group(group_id=group_id)
    if existing:
        current = existing[0].result_json
        if current == result:
            return
        if (
            isinstance(current, dict)
            and current.get("approval_id") == record.approval_id
            and current.get("resolution_status") == resolution_status
            and current.get("run_status") == "pending"
            and run_status is not None
        ):
            await uow.evidence_outbox.update_group_result(
                group_id=group_id,
                result=result,
            )
            return
        raise RuntimeError("ordered approval evidence result is inconsistent")
    reserved = await uow.event_capacity.reserve(
        run_id=record.run_id,
        operation_kind=EvidenceOperationKind.APPROVAL_RESOLUTION,
    )
    await uow.evidence_outbox.stage_ordered_group(
        tenant_id=record.tenant_id,
        run_id=record.run_id,
        group_id=group_id,
        items=[
            {
                "event_id": f"approval-resolution:{record.approval_id}",
                "operation_kind": "approval_resolution",
                "sequence_in_group": 1,
                "reserved_event_count": reserved,
                "result": result,
            },
            {
                "event_id": f"run-terminal:{record.run_id}",
                "operation_kind": "run_terminal",
                "sequence_in_group": 2,
                "reserved_event_count": 0,
                "result": result,
            },
        ],
    )


async def complete_approval_evidence_group(
    storage: SQLAlchemyStorage,
    event_bus: EventBus,
    *,
    record: ApprovalRecord,
) -> None:
    """两项已按序 durable 后完成 group，并为 local sink 对账真实 seq。"""

    group_id = approval_evidence_group_id(record.approval_id)
    resolution = await event_bus.event_by_id(
        run_id=record.run_id,
        event_id=f"approval-resolution:{record.approval_id}",
    )
    terminal = await event_bus.event_by_id(
        run_id=record.run_id,
        event_id=f"run-terminal:{record.run_id}",
    )
    if resolution is None or terminal is None or not terminal.terminal:
        raise RuntimeError("ordered approval evidence is incomplete")
    if resolution.seq >= terminal.seq:
        raise RuntimeError("approval resolution must precede run terminal")
    async with storage.uow() as uow:
        group = await uow.evidence_outbox.ordered_group(group_id=group_id)
        if [item.event_id for item in group] != [resolution.event_id, terminal.event_id]:
            raise RuntimeError("ordered approval evidence group is invalid")
        states = {item.state for item in group}
        if states == {"published"}:
            return
        if states != {"result_persisted"}:
            raise RuntimeError("ordered approval evidence group state is invalid")
        if not event_bus.capacity_managed:
            await uow.event_capacity.record_local_published(
                run_id=record.run_id,
                reserved_event_count=sum(item.reserved_event_count for item in group),
                highest_persisted_seq=terminal.seq,
                terminal=True,
            )
        await uow.evidence_outbox.mark_group_published(group_id=group_id)
        await uow.commit()


async def publish_resolution_evidence(
    event_bus: EventBus,
    *,
    actor: IdentityContext,
    record: ApprovalRecord,
    request_id: str | None,
) -> None:
    """用 approval 级稳定 id 发布或复用唯一 resolution event。

    状态已提交而首次响应丢失时，后续请求的 ``request_id`` 属于恢复调用，不得
    改写首次 resolution evidence；先验证已存在事件与持久化 approval 语义一致，
    只有 evidence 确实缺失时才补写。
    """

    event_id = f"approval-resolution:{record.approval_id}"
    payload = redact_secrets(approval_evidence(record))
    existing = await event_bus.event_by_id(run_id=record.run_id, event_id=event_id)
    if existing is not None:
        stable_semantics = (
            existing.tenant_id == record.tenant_id
            and existing.run_id == record.run_id
            and existing.agent_id == record.agent_id
            and existing.user_id == record.resolved_by
            and existing.event_type == CanonicalEventType.APPROVAL_RESOLVED
            and existing.event_version == "1.0"
            and existing.payload == payload
            and existing.payload_ref is None
            and existing.payload_checksum is None
            and existing.raw_event_ref is None
            and existing.terminal is False
            and existing.visibility == "internal"
            and existing.trace_id == record.trace_id
            and existing.record_scope == "run"
            and existing.parent_run_id is None
            and existing.span_id is None
            and existing.request_id == request_id
        )
        if not stable_semantics:
            raise EventSinkReplayConflict("event replay envelope does not match persisted event")
        return
    await event_bus.publish(
        tenant_id=record.tenant_id,
        run_id=record.run_id,
        agent_id=record.agent_id,
        user_id=record.resolved_by or actor.user_id,
        event_type=CanonicalEventType.APPROVAL_RESOLVED,
        payload=payload,
        request_id=request_id,
        trace_id=record.trace_id,
        event_id=event_id,
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
    resolution_request_id: str,
) -> RunResult:
    """补齐 denied terminal/resolution，重复执行仍只保留一份 evidence。"""

    evidence_record = resolved_approval_record(record, status="denied")
    async with storage.uow() as uow:
        await stage_approval_evidence_group(
            uow,
            record=record,
            resolution_status="denied",
            run_status=RunStatus.FAILED,
        )
        run = await uow.runs.get(record.run_id)
        await uow.commit()
    if run is None or run.tenant_id != actor.tenant_id:
        raise LookupError(f"run not found: {record.run_id}")
    run_result = RunResult(run_id=run.id, status=RunStatus(run.status))
    if run_result.status != RunStatus.FAILED:
        if run_result.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            raise RuntimeError(f"denied approval has incompatible terminal run: {record.run_id}")
        try:
            run_result = await orchestrator.fail_run(
                record.run_id,
                reason="approval denied",
                identity=actor,
                defer_terminal=True,
            )
        except InvalidRunTransition:
            run_result = await orchestrator.get_run(record.run_id, identity=actor)
            if run_result.status != RunStatus.FAILED:
                raise
    await publish_resolution_evidence(
        event_bus,
        actor=actor,
        record=evidence_record,
        request_id=resolution_request_id,
    )
    run_result = await orchestrator.get_run(record.run_id, identity=actor)
    await complete_approval_evidence_group(
        storage,
        event_bus,
        record=record,
    )
    async with storage.uow() as uow:
        await uow.approvals.mark_denied_evidence_complete(
            approval_id=record.approval_id,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
        )
        await uow.commit()
    return run_result
