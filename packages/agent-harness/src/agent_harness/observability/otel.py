"""Provider-neutral CanonicalEvent to OTel mapping facade."""

from __future__ import annotations

from typing import Any

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEvent


class OTelEventMapping(HarnessDTO):
    name: str
    attributes: dict[str, Any]


def map_event_to_otel(event: CanonicalEvent) -> OTelEventMapping:
    # OTel mapping 只输出稳定 envelope 字段。provider 原始细节留在
    # raw_event_ref/payload_ref 后面，不直接泄露进观测标签。
    return OTelEventMapping(
        name=f"agent_harness.{event.event_type.value}",
        attributes={
            "event_id": event.event_id,
            "tenant_id": event.tenant_id,
            "user_id": event.user_id,
            "run_id": event.run_id,
            "parent_run_id": event.parent_run_id,
            "agent_id": event.agent_id,
            "event_type": event.event_type.value,
            "seq": event.seq,
            "terminal": event.terminal,
            "trace_id": event.trace_id,
            "span_id": event.span_id,
        },
    )
