"""真实 PostgreSQL/Redis service 的 RUN-006 传输探针。"""

from __future__ import annotations

import json
from typing import Any, cast

from service_http_smoke import stream
from service_smoke_support import postgres_counts, stream_length

RUN_STREAM = "agent-harness:service:runs:stream"


def _events(body: bytes) -> list[dict[str, Any]]:
    """解析业务 frame，并逐帧核对 id/event/data 绑定。"""

    parsed: list[dict[str, Any]] = []
    for frame in body.decode("utf-8").split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        data = cast(dict[str, Any], json.loads(fields["data"]))
        if int(fields["id"]) != data["seq"] or fields["event"] != data["event_type"]:
            raise RuntimeError("SSE id/event/data did not bind one CanonicalEvent")
        parsed.append(data)
    return parsed


def run_sse_smoke(
    env: dict[str, str],
    *,
    base_url: str,
    token: str,
    run_id: str,
) -> dict[str, object]:
    """验证 PostgreSQL reader 的初始读取、恢复、EOF 与零业务副作用。"""

    before_counts = postgres_counts(env)
    before_stream = stream_length(env, RUN_STREAM)
    status, headers, body = stream(
        base_url,
        f"/api/v1/runs/{run_id}/events/stream",
        token=token,
        request_id="request-service-sse",
    )
    events = _events(body)
    if status != 200 or not headers.get("content-type", "").startswith("text/event-stream"):
        raise RuntimeError("service RUN-006 did not return text/event-stream")
    if not events or not events[-1].get("terminal"):
        raise RuntimeError("service RUN-006 did not end on a terminal CanonicalEvent")
    sequences = [cast(int, item["seq"]) for item in events]
    if sequences != sorted(set(sequences)):
        raise RuntimeError("service RUN-006 sequence was not strictly increasing")

    resumed_status, _, resumed_body = stream(
        base_url,
        f"/api/v1/runs/{run_id}/events/stream",
        token=token,
        last_event_id=sequences[0],
        request_id="request-service-sse-resume",
    )
    resumed = _events(resumed_body)
    eof_status, _, eof_body = stream(
        base_url,
        f"/api/v1/runs/{run_id}/events/stream",
        token=token,
        last_event_id=sequences[-1],
        request_id="request-service-sse-eof",
    )
    invalid_status, invalid_headers, _ = stream(
        base_url,
        f"/api/v1/runs/{run_id}/events/stream?after_seq=0",
        token=token,
        request_id="request-service-sse-invalid",
    )
    if resumed_status != 200 or [item["seq"] for item in resumed] != sequences[1:]:
        raise RuntimeError("service RUN-006 exclusive cursor resume drifted")
    if eof_status != 200 or eof_body != b"":
        raise RuntimeError("service RUN-006 consumed terminal did not return immediate EOF")
    if invalid_status != 422 or not invalid_headers.get("content-type", "").startswith(
        "application/json"
    ):
        raise RuntimeError("service RUN-006 accepted the forbidden after_seq query cursor")
    if postgres_counts(env) != before_counts or stream_length(env, RUN_STREAM) != before_stream:
        raise RuntimeError("service RUN-006 read changed PostgreSQL or Redis business state")

    return {
        "store": "postgresql",
        "queue": "redis",
        "run_id": run_id,
        "event_count": len(events),
        "first_seq": sequences[0],
        "terminal_seq": sequences[-1],
        "resume_count": len(resumed),
        "consumed_terminal_eof": True,
        "after_seq_status": invalid_status,
        "read_side_effects": 0,
    }


__all__ = ["run_sse_smoke"]
