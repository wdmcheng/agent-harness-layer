"""流式 delta/completed 的耐久意图、发布与容量状态转换。"""

from __future__ import annotations

import hashlib
from typing import Protocol

from agent_harness.events import CanonicalEvent, CanonicalEventType
from agent_harness.models._streaming_contracts import StreamingRuntime
from agent_harness.models.providers import ModelResponse
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.shared_budget import BudgetOperationOwnership
from agent_harness.storage.stream_evidence_repositories import (
    stream_completed_event_id,
    stream_delta_event_id,
)


class MutateStreamingUow(Protocol):
    """首 delta 持久化事务内允许执行的窄 mutation 回调。"""

    async def __call__(self, uow: SQLAlchemyUnitOfWork) -> None: ...


async def persist_delta(
    runtime: StreamingRuntime,
    *,
    context: UsageEvidenceContext,
    usage_call_id: str,
    ordinal: int,
    text: str,
    attempt: int = 1,
    mutate_uow: MutateStreamingUow | None = None,
) -> CanonicalEvent:
    """固化 delta intent；可在同一 UoW 同步提交首 delta 的额外围栏。"""

    intent = CanonicalEvent(
        event_id=stream_delta_event_id(usage_call_id, ordinal),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        agent_id=context.agent_id,
        event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
        seq=0,
        payload={
            "correlation": {"usage_call_id": usage_call_id},
            "attempt": attempt,
            "chunk_ordinal": ordinal,
            "text": text,
        },
        visibility="public",
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    async with runtime.storage.uow() as uow:
        if mutate_uow is not None:
            await mutate_uow(uow)
        await uow.evidence_outbox.persist_stream_event(intent)
        await uow.commit()
    return intent


async def persist_completed_and_final(
    runtime: StreamingRuntime,
    *,
    context: UsageEvidenceContext,
    usage_call_id: str,
    chunks: list[str],
    evidence: ModelUsageEvidence,
    outcome: str,
    error_code: str | None,
    ownership: BudgetOperationOwnership | None,
    response: ModelResponse,
    attempt: int = 1,
) -> CanonicalEvent:
    """同事务固化 completed、usage final、共享预算与未用槽位释放。"""

    text = "".join(chunks)
    encoded = text.encode("utf-8")
    intent = CanonicalEvent(
        event_id=stream_completed_event_id(usage_call_id),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        agent_id=context.agent_id,
        event_type=CanonicalEventType.MODEL_OUTPUT_COMPLETED,
        seq=0,
        payload={
            "correlation": {"usage_call_id": usage_call_id},
            "attempt": attempt,
            "chunk_count": len(chunks),
            "text_utf8_bytes": len(encoded),
            "text_sha256": hashlib.sha256(encoded).hexdigest(),
        },
        visibility="public",
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    async with runtime.storage.uow() as uow:
        await uow.evidence_outbox.cancel_unused_stream(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            usage_call_id=usage_call_id,
            used_delta_count=len(chunks),
            keep_completed=True,
        )
        await uow.evidence_outbox.persist_stream_event(intent)
        await runtime.persist_final_in_uow(
            uow=uow,
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome=outcome,
            error_code=error_code,
            ownership=ownership,
            response=response,
        )
        await uow.commit()
    return intent


async def publish_persisted_stream(
    runtime: StreamingRuntime,
    intent: CanonicalEvent,
) -> CanonicalEvent:
    """以原 timestamp 和稳定 event id 补投 intent，随后完成 outbox 状态转换。"""

    persisted = await runtime.event_bus.publish(
        tenant_id=intent.tenant_id,
        run_id=intent.run_id,
        agent_id=intent.agent_id,
        user_id=intent.user_id,
        parent_run_id=intent.parent_run_id,
        event_type=intent.event_type,
        payload=intent.payload,
        terminal=False,
        visibility="public",
        request_id=intent.request_id,
        trace_id=intent.trace_id,
        span_id=intent.span_id,
        event_id=intent.event_id,
        timestamp=intent.timestamp,
    )
    if (
        runtime.timing_observer is not None
        and intent.event_type is CanonicalEventType.MODEL_OUTPUT_DELTA
    ):
        # EventBus 返回时 public sink 已经提交；必须在任何可选 telemetry await
        # 前记录该边界，live reader 再以此建立 commit -> client 的 happens-before。
        runtime.timing_observer("committed_delta")
    if runtime.telemetry is not None:
        await runtime.telemetry.publish_event(persisted)
    async with runtime.storage.uow() as uow:
        if not runtime.event_bus.capacity_managed:
            await uow.event_capacity.record_local_published(
                run_id=intent.run_id,
                reserved_event_count=1,
                highest_persisted_seq=persisted.seq,
            )
        await uow.evidence_outbox.mark_event_published(event_id=intent.event_id)
        await uow.commit()
    return persisted


__all__ = [
    "MutateStreamingUow",
    "persist_completed_and_final",
    "persist_delta",
    "publish_persisted_stream",
]
