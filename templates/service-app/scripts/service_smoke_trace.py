"""把 PostgreSQL inspect 事件导出为受限 service smoke 证据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4


def write_service_trace(env: dict[str, str], completed: dict[str, Any]) -> None:
    """原子写入真实持久化事件，拒绝空事件或非对象事件。

    Service profile 的事件 sink 是 PostgreSQL，配置中的 observability path 不会产生
    local JSONL；CI trace 必须从已经过相关性核验的持久化事件导出，不能伪造空文件。
    """

    raw_events = completed.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise RuntimeError("service smoke PostgreSQL trace is empty")
    events = cast(list[object], raw_events)
    if any(not isinstance(event, dict) for event in events):
        raise RuntimeError("service smoke PostgreSQL trace contains an invalid event")

    trace_path = Path(env["SERVICE_APP_SMOKE_DIR"]) / "trace.jsonl"
    temporary = trace_path.with_name(f".{trace_path.name}.{uuid4().hex}.tmp")
    records = [
        {
            "schema_version": "service-smoke-trace/v1",
            "source": "postgresql",
            "run_id": completed["run_id"],
            "tenant_id": completed["tenant_id"],
            "event": cast(dict[str, Any], event),
        }
        for event in events
    ]
    try:
        temporary.write_text(
            "".join(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        temporary.chmod(0o640)
        temporary.replace(trace_path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["write_service_trace"]
