"""Service smoke 的 HTTP 请求、轮询与运行提交辅助函数。"""

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
    """发送 JSON HTTP 请求，并将预期的 HTTP 错误保留为状态码和响应体。

    smoke 需要断言认证和校验失败的精确 HTTP 语义，因此不能让 ``HTTPError``
    中断控制流。网络级异常仍向上传播，由上层边界诊断统一脱敏处理。
    """
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


def stream(
    base_url: str,
    path: str,
    *,
    token: str | None = None,
    last_event_id: int | None = None,
    request_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """读取一个会终止的 SSE 响应，保留 headers 与原始 frame bytes。"""

    headers = {"Accept": "text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    http_request = urllib.request.Request(
        f"{base_url}{path}",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=5) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            exc.read(),
        )


def wait_for(
    description: str,
    predicate: Callable[[], Any],
    *,
    timeout_seconds: float = 45,
) -> Any:
    """在有限时间内轮询条件，并在超时信息中保留最后一次可诊断状态。

    可恢复的网络、JSON 与业务暂态异常会被视为尚未就绪；其他编程错误不吞掉，
    避免 smoke 把脚本缺陷误报成普通服务超时。
    """
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
    """轮询运行详情直到达到目标状态，并补充恢复 handler 标记帮助定位超时。"""
    latest: dict[str, Any] = {}

    def poll() -> dict[str, Any] | None:
        """读取一次运行快照；只有 HTTP 成功且状态匹配时才结束等待。"""
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
    """等待运行首次暴露审批记录，并返回其稳定审批 ID。"""

    def poll() -> str | None:
        """读取审批列表；空列表表示运行尚未到达可审批状态。"""
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
    """提交异步运行，并断言 API 返回的是可入队的创建结果。

    idempotency key 与 request ID 由调用方生成，以便后续 Redis、PostgreSQL 和
    事件证据使用同一关联键；任何非创建响应都应立即停止 smoke。
    """
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
