"""Service smoke 的 HTTP 请求、轮询与 run submission helper。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
    request_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    payload = None if body is None else json.dumps(body).encode()
    http_request = urllib.request.Request(
        f"{base_url}{path}",
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(http_request, timeout=5) as response:
            return response.status, cast(dict[str, Any], json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        return exc.code, cast(dict[str, Any], json.loads(exc.read()))


def wait_for(
    description: str,
    predicate: Callable[[], Any],
    *,
    timeout_seconds: float = 45,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"timeout waiting for {description}: {last}")


def wait_run_status(
    base_url: str,
    token: str,
    run_id: str,
    status: str,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}

    def poll() -> dict[str, Any] | None:
        nonlocal latest
        code, payload = request(base_url, "GET", f"/api/v1/runs/{run_id}", token=token)
        latest = payload
        return payload if code == 200 and payload.get("status") == status else None

    try:
        return cast(
            dict[str, Any],
            wait_for(f"run {run_id} status={status}", poll, timeout_seconds=60),
        )
    except RuntimeError as exc:
        marker = Path(os.environ.get("SERVICE_APP_SMOKE_DIR", "")) / "recovery-handler.json"
        marker_payload = marker.read_text(encoding="utf-8") if marker.exists() else "missing"
        raise RuntimeError(
            f"run status timeout: latest={latest} recovery_handler={marker_payload}"
        ) from exc


def approval_id(base_url: str, token: str, run_id: str) -> str:
    def poll() -> str | None:
        code, payload = request(
            base_url,
            "GET",
            f"/api/v1/runs/{run_id}/approvals",
            token=token,
        )
        approvals = payload.get("approvals", [])
        return approvals[0]["approval_id"] if code == 200 and approvals else None

    return cast(str, wait_for(f"approval for {run_id}", poll))


def submit(
    base_url: str,
    token: str,
    *,
    agent_id: str,
    input_payload: dict[str, object],
    idempotency_key: str,
    request_id: str,
) -> dict[str, Any]:
    code, payload = request(
        base_url,
        "POST",
        f"/api/v1/agents/{agent_id}/runs",
        token=token,
        request_id=request_id,
        body={"input": input_payload, "idempotency_key": idempotency_key},
    )
    if code != 202 or payload.get("status") != "created":
        raise RuntimeError(f"RUN-001 enqueue failed: status={code} body={payload}")
    return payload
