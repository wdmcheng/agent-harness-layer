"""PostgreSQL sink 内部的流式 outbox 绑定与前驱结算校验。"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_harness.events.capacity import (
    StreamCapacityBinding,
    StreamCapacitySettlement,
    validate_stream_capacity_outbox,
)
from agent_harness.events.types import CanonicalEvent
from agent_harness.storage.models import RunEvidenceOutboxModel
from agent_harness.storage.stream_evidence_repositories import (
    require_complete_settled_predecessors,
    stream_group_id,
)


async def validate_stream_usage_final(
    connection: AsyncConnection,
    *,
    event: CanonicalEvent,
    usage_call_id: str,
) -> None:
    """锁定完整 65 槽并证明 usage final 的 stream 前驱已经全部结算。"""

    payload = event.payload
    outcome = payload.get("outcome") if isinstance(payload, Mapping) else None
    if not isinstance(outcome, str) or outcome not in {"completed", "cancelled", "failed"}:
        raise ValueError("stream usage final requires a valid outcome")
    group_rows = await connection.execute(
        select(
            RunEvidenceOutboxModel.sequence_in_group,
            RunEvidenceOutboxModel.state,
        )
        .where(RunEvidenceOutboxModel.group_id == stream_group_id(usage_call_id))
        .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
        .with_for_update()
    )
    stream_states = list(group_rows.all())
    if [row[0] for row in stream_states] != list(range(1, 66)):
        raise LookupError("stream evidence group is incomplete")
    completed_state = str(stream_states[64][1])
    if outcome == "completed" and completed_state != "published":
        raise LookupError("completed stream evidence is not published")
    if outcome != "completed" and completed_state != "cancelled":
        raise LookupError("interrupted stream completed placeholder is not cancelled")
    if any(str(row[1]) not in {"published", "cancelled"} for row in stream_states):
        raise LookupError("stream evidence predecessor is not settled")


async def validate_stream_event_capacity(
    connection: AsyncConnection,
    *,
    event: CanonicalEvent,
    binding: StreamCapacityBinding,
) -> int:
    """锁定 stream intent，核对 payload/identity，并验证所有前驱已结算。"""

    stream_outbox = await connection.execute(
        select(
            RunEvidenceOutboxModel.tenant_id,
            RunEvidenceOutboxModel.run_id,
            RunEvidenceOutboxModel.event_id,
            RunEvidenceOutboxModel.operation_kind,
            RunEvidenceOutboxModel.state,
            RunEvidenceOutboxModel.reserved_event_count,
            RunEvidenceOutboxModel.group_id,
            RunEvidenceOutboxModel.sequence_in_group,
            RunEvidenceOutboxModel.result_json,
        )
        .where(RunEvidenceOutboxModel.event_id == event.event_id)
        .with_for_update()
    )
    stream_row = stream_outbox.one_or_none()
    stream_settlement = (
        StreamCapacitySettlement(
            tenant_id=str(stream_row[0]),
            run_id=str(stream_row[1]),
            event_id=str(stream_row[2]),
            operation_kind=str(stream_row[3]),
            state=str(stream_row[4]),
            reserved_event_count=int(stream_row[5]),
            group_id=str(stream_row[6]) if stream_row[6] is not None else None,
            sequence_in_group=int(stream_row[7]) if stream_row[7] is not None else None,
            result_json=stream_row[8],
        )
        if stream_row is not None
        else None
    )
    reserved_event_count = validate_stream_capacity_outbox(
        event=event,
        binding=binding,
        outbox=stream_settlement,
    )
    predecessors = await connection.execute(
        select(
            RunEvidenceOutboxModel.sequence_in_group,
            RunEvidenceOutboxModel.state,
        )
        .where(
            RunEvidenceOutboxModel.group_id == binding.group_id,
            RunEvidenceOutboxModel.sequence_in_group < binding.sequence_in_group,
        )
        .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
        .with_for_update()
    )
    require_complete_settled_predecessors(
        current_sequence=binding.sequence_in_group,
        predecessors=[(sequence, str(state)) for sequence, state in predecessors.all()],
    )
    return reserved_event_count


__all__ = ["validate_stream_event_capacity", "validate_stream_usage_final"]
