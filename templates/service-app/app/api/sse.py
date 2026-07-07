"""把 CanonicalEvent payload 格式化成 SSE 帧。"""

from __future__ import annotations

import json

from agent_harness.events import CanonicalEvent


def format_sse_event(event: CanonicalEvent) -> str:
    data = json.dumps(event.to_payload(), separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.event_type.value}\ndata: {data}\n\n"
