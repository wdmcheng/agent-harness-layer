"""local profile 的 CLI-EVT-001 与 RUN-006 transport smoke。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

FINGERPRINT_ENV = "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY"


def validate_local_event_transports(
    *,
    root: Path,
    service_app: Path,
    environment: dict[str, str],
    dsn: str,
    events_path: Path,
    run_id: str,
    events: list[dict[str, object]],
) -> dict[str, int]:
    """在同一 local event store 上验证 CLI NDJSON、SSE 与 terminal resume。"""

    streamed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "events",
            "stream",
            run_id,
            "--profile",
            "local",
            "--profiles-dir",
            str(service_app / "configs" / "profiles"),
            "--storage-dsn",
            dsn,
            "--events-path",
            str(events_path),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if streamed.returncode != 0:
        raise RuntimeError(f"events stream failed: {streamed.stderr.strip()}")
    cli_events = [json.loads(line) for line in streamed.stdout.splitlines() if line]
    visible_events = [item for item in events if item.get("visibility") == "public"]
    if cli_events != visible_events:
        raise RuntimeError("CLI stream did not preserve canonical public event bytes")

    previous_fingerprint = os.environ.get(FINGERPRINT_ENV)
    os.environ[FINGERPRINT_ENV] = environment[FINGERPRINT_ENV]
    try:
        app = create_app(
            profile="local",
            profiles_dir=service_app / "configs" / "profiles",
            storage_dsn=dsn,
            events_path=events_path,
        )
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={"Accept": "text/event-stream", "X-Request-Id": "req-smoke-sse"},
            )
            terminal_seq = int(str(visible_events[-1]["seq"]))
            consumed = client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={
                    "Accept": "text/event-stream",
                    "Last-Event-ID": str(terminal_seq),
                    "X-Request-Id": "req-smoke-sse-resume",
                },
            )
    finally:
        if previous_fingerprint is None:
            del os.environ[FINGERPRINT_ENV]
        else:
            os.environ[FINGERPRINT_ENV] = previous_fingerprint

    if response.status_code != 200:
        raise RuntimeError(f"SSE stream returned HTTP {response.status_code}")
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        raise RuntimeError("SSE stream did not return text/event-stream")
    sse_events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    if sse_events != visible_events:
        raise RuntimeError("SSE stream and CLI did not expose the same canonical public events")
    if consumed.status_code != 200 or consumed.content != b"":
        raise RuntimeError("SSE terminal cursor did not handshake then immediately EOF")
    return {"public_events": len(visible_events), "terminal_seq": terminal_seq}


__all__ = ["validate_local_event_transports"]
