"""验证 service worker 硬崩溃、receipt fencing 与消息重领。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from service_http_smoke import submit, wait_for, wait_run_status
from service_smoke_operations import assert_stale_receipt, inspect_run
from service_smoke_support import (
    compose,
    latest_stream_message,
    reclaim_receipts_match,
    redis_json,
    run,
)

STREAM = "agent-harness:service:runs:stream"
GROUP = "agent-harness-workers"


@dataclass(frozen=True)
class ReclaimEvidence:
    """worker 崩溃与重领场景的稳定身份，供后续传输和证据阶段复核。"""

    expected: dict[str, str]
    message_id: str
    worker_b_receipt: dict[str, Any]
    marker: dict[str, Any]
    run_id: str
    completed: dict[str, Any]


def run_reclaim_scenario(
    env: dict[str, str],
    *,
    base_url: str,
    token: str,
    tenant_id: str,
) -> ReclaimEvidence:
    """注入 owner 持久化后的硬崩溃，并验证另一 worker 安全重领。"""

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "pickup-reclaim"
    request_id = f"request-{uuid4()}"
    idempotency_key = f"smoke-{uuid4()}"
    submitted = submit(
        base_url,
        token,
        agent_id="examples.ticket_triage",
        input_payload={"text": "production outage: checkout is down"},
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    run_id = cast(str, submitted["run_id"])
    message_id, message = latest_stream_message(env, STREAM)
    expected = {
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "tenant_id": tenant_id,
        "run_id": run_id,
    }
    if any(message.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Redis queue correlation mismatch: {message}")

    worker_a = f"{env['SERVICE_APP_COMPOSE_PROJECT']}-worker-a"
    compose(
        env,
        "run",
        "-d",
        "--name",
        worker_a,
        "--no-deps",
        "-e",
        "SERVICE_APP_SMOKE_CRASH_AFTER_OWNER=1",
        "-e",
        "SERVICE_APP_READY_FILE=",
        "-e",
        "SERVICE_APP_SMOKE_CRASH_MARKER=/smoke/crash-owner.json",
        "-e",
        "SERVICE_APP_SMOKE_RECEIPT_MARKER=/smoke/worker-a-receipt.json",
        "-e",
        "SERVICE_APP_SMOKE_RECLAIM_RELEASE=",
        "worker",
    )

    def crashed() -> bool:
        """判断注入 owner 后崩溃的 worker 是否以约定退出码结束。"""

        result = run(
            ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}", worker_a],
            env=env,
            check=False,
        )
        return result.stdout.strip() == "exited|23"

    wait_for("worker A hard crash", crashed)
    smoke_dir = Path(env["SERVICE_APP_SMOKE_DIR"])
    marker = json.loads((smoke_dir / "crash-owner.json").read_text(encoding="utf-8"))
    worker_a_receipt = json.loads((smoke_dir / "worker-a-receipt.json").read_text(encoding="utf-8"))
    crashed_state = inspect_run(env, run_id)
    if crashed_state["status"] != "running" or crashed_state["owner_id"] != marker["owner_id"]:
        raise RuntimeError("worker A exited before application owner was durable")
    execution_expected = {**expected, "message_id": message_id}
    if any(crashed_state.get(key) != value for key, value in execution_expected.items()):
        raise RuntimeError("PostgreSQL execution correlation mismatch after worker A crash")
    pending = cast(list[list[object]], redis_json(env, "XPENDING", STREAM, GROUP, "-", "+", "10"))
    if (
        not pending
        or pending[0][0] != worker_a_receipt["message_id"]
        or pending[0][1] != worker_a_receipt["consumer_id"]
        or int(cast(int, pending[0][3])) != worker_a_receipt["delivery_count"]
    ):
        raise RuntimeError(f"worker A did not leave the original fenced receipt pending: {pending}")

    time.sleep(float(env["SERVICE_APP_RECLAIM_IDLE_SECONDS"]) + 0.25)
    compose(env, "up", "-d", "--wait", "worker")
    worker_b_receipt_path = smoke_dir / "worker-b-receipt.json"
    wait_for("worker B reclaim receipt", worker_b_receipt_path.exists)
    worker_b_receipt = json.loads(worker_b_receipt_path.read_text(encoding="utf-8"))
    if not reclaim_receipts_match(message_id, worker_a_receipt, worker_b_receipt):
        raise RuntimeError(f"worker B reclaim receipt mismatch: {worker_b_receipt}")
    reclaimed_pending = cast(
        list[list[object]], redis_json(env, "XPENDING", STREAM, GROUP, "-", "+", "10")
    )
    if (
        not reclaimed_pending
        or reclaimed_pending[0][1] != worker_b_receipt["consumer_id"]
        or int(cast(int, reclaimed_pending[0][3])) != worker_b_receipt["delivery_count"]
    ):
        raise RuntimeError("worker B reclaim ownership was not pending during fencing check")
    if not assert_stale_receipt(
        env,
        stream=worker_a_receipt["stream"],
        group=worker_a_receipt["group"],
        message_id=worker_a_receipt["message_id"],
        consumer_id=worker_a_receipt["consumer_id"],
        delivery_count=worker_a_receipt["delivery_count"],
    ):
        raise RuntimeError("worker A stale receipt was not rejected")
    (smoke_dir / "reclaim-release").touch()
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "dbos-event-wait-completed"
    wait_run_status(base_url, token, run_id, "completed")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "dbos-event-inspect"
    try:
        completed = inspect_run(env, run_id)
    except RuntimeError as exc:
        if str(exc).startswith("service.inspect."):
            env["SERVICE_APP_SMOKE_BOUNDARY"] = str(exc)
        raise
    return ReclaimEvidence(
        expected=expected,
        message_id=message_id,
        worker_b_receipt=worker_b_receipt,
        marker=marker,
        run_id=run_id,
        completed=completed,
    )
