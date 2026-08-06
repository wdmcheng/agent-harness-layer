"""模型工具循环 event intent 的耐久恢复与 needs-review 围栏。"""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field, ValidationError, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events.bus import EventBus
from agent_harness.events.types import CanonicalEventType
from agent_harness.models.structured import structured_digest
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


class ModelToolLoopEventRecoveryError(RuntimeError):
    """耐久event intent未知或冲突，保留预约并阻止循环继续。"""

    code = "model.tool_loop_needs_review"

    def __init__(self) -> None:
        super().__init__(self.code)


class ModelToolLoopEventPublishPending(RuntimeError):
    """exact intent已耐久但发布确认失败，允许后续runtime只补投同一envelope。"""

    code = "model.tool_loop_event_publish_pending"

    def __init__(self, *, group_id: str, message: str) -> None:
        super().__init__(message)
        self.group_id = group_id


class _ModelToolLoopEventCorrelation(HarnessDTO):
    """所有模型工具事件共用的 exact 恢复坐标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    loop_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_ordinal: int = Field(gt=0, strict=True)
    tool_call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_usage_call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _DurableModelToolLoopEventIntent(HarnessDTO):
    """outbox保存的完整可重建CanonicalEvent意图，不包含seq或时间戳。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["model-tool-loop-event-intent-v1"] = "model-tool-loop-event-intent-v1"
    event_version: Literal["1.0"] = "1.0"
    tenant_id: str
    run_id: str
    agent_id: str
    identity_id: str
    request_id: str | None
    trace_id: str
    event_type: CanonicalEventType
    payload: dict[str, Any]
    release_after: int = Field(ge=0, le=1, strict=True)

    @model_validator(mode="after")
    def validate_payload_identity(self) -> _DurableModelToolLoopEventIntent:
        """event payload版本与全部loop坐标必须可由恢复器封闭解析。"""

        if self.payload.get("schema_version") != "model-tool-loop-event-v1":
            raise ValueError("model tool loop event payload schema is unknown")
        _ModelToolLoopEventCorrelation.model_validate(self.payload.get("correlation"))
        return self


async def recover_group(*, storage: SQLAlchemyStorage, event_bus: EventBus, group_id: str) -> int:
    """按耐久顺序补投同一event envelope，不据此重放任何业务副作用。"""

    async with storage.uow() as uow:
        rows = await uow.evidence_outbox.ordered_group(group_id=group_id)
        snapshots = [
            {
                "tenant_id": item.tenant_id,
                "run_id": item.run_id,
                "event_id": item.event_id,
                "operation_kind": item.operation_kind,
                "state": item.state,
                "sequence": item.sequence_in_group,
                "result": None if item.result_json is None else dict(item.result_json),
            }
            for item in rows
        ]
    if len(snapshots) != 2 or [item["sequence"] for item in snapshots] != [1, 2]:
        raise ModelToolLoopEventRecoveryError
    recovered = 0
    for item in snapshots:
        state = item["state"]
        if state == "reserved":
            break
        raw = item["result"]
        reason = _unknown_intent_reason(raw)
        if reason is not None:
            await _fence_unknown_event(storage=storage, item=item, raw=raw, reason=reason)
            raise ModelToolLoopEventRecoveryError
        try:
            intent = _DurableModelToolLoopEventIntent.model_validate(raw)
            _validate_recovery_item(item=item, intent=intent)
        except (TypeError, ValidationError, ValueError):
            await _fence_unknown_event(
                storage=storage,
                item=item,
                raw=raw,
                reason="event_evidence_missing",
            )
            raise ModelToolLoopEventRecoveryError from None
        if state == "published":
            continue
        if state != "result_persisted":
            await _fence_unknown_event(
                storage=storage,
                item=item,
                raw=raw,
                reason="event_evidence_missing",
            )
            raise ModelToolLoopEventRecoveryError
        await event_bus.publish(
            tenant_id=intent.tenant_id,
            run_id=intent.run_id,
            agent_id=intent.agent_id,
            user_id=intent.identity_id,
            event_type=intent.event_type,
            payload=intent.payload,
            request_id=intent.request_id,
            trace_id=intent.trace_id,
            event_id=cast(str, item["event_id"]),
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.mark_event_published(event_id=cast(str, item["event_id"]))
            if intent.release_after:
                await uow.event_capacity.release(
                    run_id=intent.run_id,
                    reserved_event_count=intent.release_after,
                )
            await uow.commit()
        recovered += 1
    return recovered


async def recover_pending_for_run(
    *,
    storage: SQLAlchemyStorage,
    event_bus: EventBus,
    run_id: str,
    loop_id: str,
) -> int:
    """生产runtime按稳定group顺序补投当前loop的event，不触发业务重放。"""

    async with storage.uow() as uow:
        pending = await uow.evidence_outbox.pending(run_id=run_id)
        group_ids: set[str] = set()
        for item in pending:
            raw = item.result_json
            if (
                item.group_id is None
                or not item.group_id.startswith("model-tool-loop:")
                or item.operation_kind
                not in {
                    EvidenceOperationKind.TOOL_INVOCATION.value,
                    EvidenceOperationKind.CONTEXT_ASSEMBLY.value,
                }
                or not isinstance(raw, dict)
            ):
                continue
            raw_mapping = cast(Mapping[str, object], raw)
            payload = raw_mapping.get("payload")
            payload_mapping = (
                cast(Mapping[str, object], payload) if isinstance(payload, dict) else None
            )
            correlation = (
                payload_mapping.get("correlation") if payload_mapping is not None else None
            )
            correlation_mapping = (
                cast(Mapping[str, object], correlation) if isinstance(correlation, dict) else None
            )
            if correlation_mapping is not None and correlation_mapping.get("loop_id") == loop_id:
                group_ids.add(item.group_id)
    recovered = 0
    for group_id in sorted(group_ids):
        recovered += await recover_group(
            storage=storage,
            event_bus=event_bus,
            group_id=group_id,
        )
    return recovered


def _unknown_intent_reason(raw: object) -> str | None:
    """在DTO解析前区分未知schema/version与一般证据损坏。"""

    if not isinstance(raw, dict):
        return "event_evidence_missing"
    payload = cast(dict[str, object], raw)
    if payload.get("schema_version") != "model-tool-loop-event-intent-v1":
        return "event_schema_unknown"
    if payload.get("event_version") != "1.0":
        return "event_version_unknown"
    return None


def _validate_recovery_item(
    *,
    item: Mapping[str, object],
    intent: _DurableModelToolLoopEventIntent,
) -> None:
    """逐值绑定outbox scope、operation kind、组序与允许的事件类型。"""

    sequence = item.get("sequence")
    operation_kind = item.get("operation_kind")
    if (
        intent.tenant_id != item.get("tenant_id")
        or intent.run_id != item.get("run_id")
        or type(sequence) is not int
    ):
        raise ValueError("event recovery scope does not match outbox")
    if operation_kind == EvidenceOperationKind.TOOL_INVOCATION.value:
        allowed = (
            {CanonicalEventType.TOOL_CALL_STARTED}
            if sequence == 1
            else {CanonicalEventType.TOOL_CALL_COMPLETED, CanonicalEventType.TOOL_CALL_FAILED}
        )
        expected_release = 0 if sequence == 1 else 1
    elif operation_kind == EvidenceOperationKind.CONTEXT_ASSEMBLY.value:
        allowed = (
            {CanonicalEventType.CONTEXT_ASSEMBLY_STARTED}
            if sequence == 1
            else {CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED}
        )
        expected_release = 0
    else:
        raise ValueError("event recovery operation kind is unsupported")
    if intent.event_type not in allowed or intent.release_after != expected_release:
        raise ValueError("event recovery type or release does not match ordered group")


async def _fence_unknown_event(
    *,
    storage: SQLAlchemyStorage,
    item: Mapping[str, object],
    raw: object,
    reason: str,
) -> None:
    """未知event保留outbox预约，并尽可能在同一UoW关闭loop与root账本。"""

    correlation: dict[str, object] = {}
    if isinstance(raw, dict):
        payload = cast(dict[str, object], raw).get("payload")
        if isinstance(payload, dict):
            candidate = cast(dict[str, object], payload).get("correlation")
            if isinstance(candidate, dict):
                correlation = cast(dict[str, object], candidate)
    loop_id = correlation.get("loop_id")
    event_id = item.get("event_id")
    tenant_id = item.get("tenant_id")
    run_id = item.get("run_id")
    if not all(isinstance(value, str) and value for value in (event_id, tenant_id, run_id)):
        return
    error_digest = structured_digest(
        {
            "schema_version": "model-tool-loop-event-recovery-error-v1",
            "event_id": event_id,
            "reason": reason,
            "loop_id": loop_id,
        }
    )
    async with storage.uow() as uow:
        await uow.evidence_outbox.mark_event_recovery_error(
            event_id=cast(str, event_id),
            error_code=reason,
        )
        if isinstance(loop_id, str):
            loop = await uow.model_tool_loops.get(cast(str, tenant_id), loop_id)
            if loop is not None and loop.status in {"active", "waiting_approval"}:
                await uow.model_tool_loops.fail(
                    tenant_id=loop.tenant_id,
                    loop_id=loop.loop_id,
                    expected_version=loop.version,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    status="needs_review",
                    error_ref=f"model-tool-loop-event-review:{error_digest}",
                    expected_status=cast(
                        Literal["active", "waiting_approval"],
                        loop.status,
                    ),
                )
        await uow.shared_budget.fence_needs_review_for_run_if_managed(
            cast(str, tenant_id),
            cast(str, run_id),
        )
        await uow.commit()


__all__ = [
    "ModelToolLoopEventPublishPending",
    "ModelToolLoopEventRecoveryError",
    "recover_group",
    "recover_pending_for_run",
]
