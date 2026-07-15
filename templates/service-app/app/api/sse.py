"""把 CanonicalEvent payload 格式化成 SSE 帧。"""

from __future__ import annotations

from agent_harness.events import CanonicalEvent, canonical_event_bytes


def format_sse_event(event: CanonicalEvent) -> str:
    data = canonical_event_bytes(event).decode("utf-8")
    return f"id: {event.seq}\nevent: {event.event_type.value}\ndata: {data}\n\n"
