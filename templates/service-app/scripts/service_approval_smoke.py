"""真实 service smoke 的审批、拒绝、事件失败与崩溃恢复场景。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from uuid import uuid4

from service_http_smoke import approval_id as _approval_id
from service_http_smoke import request as _request
from service_http_smoke import submit as _submit
from service_http_smoke import wait_for as _wait_for
from service_http_smoke import wait_run_status as _wait_run_status
from service_smoke_operations import (
    inspect_run,
    install_approval_event_write_failure,
    postgres_approval_evidence,
    remove_approval_event_write_failure,
    run_queue_ack_evidence,
    run_queue_pending_evidence,
)
from service_smoke_support import (
    compose,
    run,
    stream_length,
)

STREAM = "agent-harness:service:runs:stream"
GROUP = "agent-harness-workers"


def _stream_length(env: dict[str, str]) -> int:
    """读取审批 smoke 使用的固定运行队列长度，用于拒绝路径零入队断言。"""
    return stream_length(env, STREAM)


def run_approval_smoke(
    env: dict[str, str],
    *,
    base_url: str,
    token: str,
) -> dict[str, object]:
    """验证审批结果、ordered evidence 与 Redis ack 的恢复顺序。"""

    # 这些固定边界只暴露控制流位置，不携带 run、approval、token 或响应内容；
    # service smoke 失败时据此区分 API 提交、等待态与 Redis 故障注入抖动。
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-approval-submit"
    approval_submit = _submit(
        base_url,
        token,
        agent_id="examples.dev_assistant",
        input_payload={
            "operation": "write",
            "path": "approved.txt",
            "content": "approved-once",
        },
        idempotency_key=f"approval-{uuid4()}",
        request_id=f"approval-submit-{uuid4()}",
    )
    approval_run = cast(str, approval_submit["run_id"])
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-approval-waiting"
    _wait_run_status(base_url, token, approval_run, "waiting")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-approval-id"
    approval_id = _approval_id(base_url, token, approval_run)
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-approval-outage"
    compose(env, "stop", "worker", "redis")
    approve_status, _ = _request(
        base_url,
        "POST",
        f"/api/v1/runs/{approval_run}/approvals/{approval_id}",
        token=token,
        body={"decision": "approved", "comment": "reviewed sk-secret-value"},
    )
    if approve_status != 503:
        raise RuntimeError(f"approval enqueue outage must return 503, got {approve_status}")
    compose(env, "start", "redis")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-approval-redis-ready"
    _wait_for(
        "Redis restart", lambda: compose(env, "exec", "-T", "redis", "redis-cli", "PING") == "PONG"
    )
    write_failure_worker = f"{env['SERVICE_APP_COMPOSE_PROJECT']}-approval-write-fail"
    ack_failure_worker = f"{env['SERVICE_APP_COMPOSE_PROJECT']}-approval-ack-fail"
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "approval-event-write-before"
    install_approval_event_write_failure(env)
    compose(
        env,
        "run",
        "-d",
        "--name",
        write_failure_worker,
        "--no-deps",
        "-e",
        "SERVICE_APP_READY_FILE=",
        "-e",
        "SERVICE_APP_SMOKE_RECEIPT_MARKER=/smoke/approval-write-fail-receipt.json",
        "-e",
        "SERVICE_APP_SMOKE_RECLAIM_RELEASE=",
        "worker",
    )

    def write_failure_exited() -> bool:
        """确认事件写前故障 worker 已异常退出，避免过早读取未落库状态。"""
        result = run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}|{{.State.ExitCode}}",
                write_failure_worker,
            ],
            env=env,
            check=False,
        )
        status, _, exit_code = result.stdout.strip().partition("|")
        return status == "exited" and exit_code not in {"", "0"}

    _wait_for("approval event write-before failure", write_failure_exited)
    write_failure_state = inspect_run(env, approval_run)
    invocation_before_recovery = write_failure_state["approvals"][0]["tool_invocation"]
    if (
        invocation_before_recovery is None
        or invocation_before_recovery["execution_state"] not in {"completed", "failed"}
        or not invocation_before_recovery["result_ref"]
        or any(event["type"] == "approval.resolved" for event in write_failure_state["events"])
    ):
        raise RuntimeError("write-before failure did not preserve one durable tool result")
    remove_approval_event_write_failure(env)

    time.sleep(float(env["SERVICE_APP_RECLAIM_IDLE_SECONDS"]) + 0.25)
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "approval-event-write-after-ack-before"
    compose(
        env,
        "run",
        "-d",
        "--name",
        ack_failure_worker,
        "--no-deps",
        "-e",
        "SERVICE_APP_READY_FILE=",
        "-e",
        f"SERVICE_APP_SMOKE_CRASH_BEFORE_ACK={approval_run}",
        "-e",
        "SERVICE_APP_SMOKE_ACK_CRASH_MARKER=/smoke/approval-ack-crash.json",
        "-e",
        "SERVICE_APP_SMOKE_RECEIPT_MARKER=/smoke/approval-ack-fail-receipt.json",
        "-e",
        "SERVICE_APP_SMOKE_RECLAIM_RELEASE=",
        "worker",
    )

    def ack_failure_exited() -> bool:
        """确认 ack 前故障发生在预定窗口，而非普通 worker 启动失败。"""
        result = run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}|{{.State.ExitCode}}",
                ack_failure_worker,
            ],
            env=env,
            check=False,
        )
        return result.stdout.strip() == "exited|24"

    _wait_for("approval evidence durable before ack crash", ack_failure_exited)
    approval_state_before_ack = inspect_run(env, approval_run)
    approval_evidence_before_ack = postgres_approval_evidence(
        approval_state_before_ack,
        expected_status="approved",
    )
    pending_before_ack = run_queue_pending_evidence(
        env,
        stream=STREAM,
        group=GROUP,
        run_id=approval_run,
    )
    invocation_after_recovery = approval_state_before_ack["approvals"][0]["tool_invocation"]
    if invocation_after_recovery != invocation_before_recovery:
        raise RuntimeError("approval recovery replaced or replayed the durable tool invocation")

    time.sleep(float(env["SERVICE_APP_RECLAIM_IDLE_SECONDS"]) + 0.25)
    compose(env, "up", "-d", "--wait", "worker")
    _wait_run_status(base_url, token, approval_run, "completed")
    approval_state = inspect_run(env, approval_run)
    if (
        approval_state["checkpoint_id"] is None
        or approval_state["approvals"][0]["status"] != "approved"
    ):
        raise RuntimeError("approval continuation did not use shared checkpoint/evidence")
    approved_path = Path(env["SERVICE_APP_SMOKE_DIR"]) / "workspace" / "approved.txt"
    if approved_path.read_text(encoding="utf-8") != "approved-once":
        raise RuntimeError("approved tool side effect was missing or duplicated")
    approval_evidence = postgres_approval_evidence(
        approval_state,
        expected_status="approved",
    )
    approval_ack: dict[str, int] = {}

    def approval_delivery_acked() -> bool:
        """轮询直至 durable 审批证据对应的 Redis receipt 已被 worker 确认。"""
        nonlocal approval_ack
        try:
            approval_ack = run_queue_ack_evidence(
                env,
                stream=STREAM,
                group=GROUP,
                run_id=approval_run,
            )
        except RuntimeError:
            return False
        return True

    _wait_for("approval Redis ack after durable evidence", approval_delivery_acked)
    if approval_state["approvals"][0]["tool_invocation"] != invocation_before_recovery:
        raise RuntimeError("final approval recovery changed the durable tool invocation")

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-deny"
    deny_submit = _submit(
        base_url,
        token,
        agent_id="examples.dev_assistant",
        input_payload={"operation": "write", "path": "denied.txt", "content": "never"},
        idempotency_key=f"deny-{uuid4()}",
        request_id=f"deny-submit-{uuid4()}",
    )
    deny_run = cast(str, deny_submit["run_id"])
    _wait_run_status(base_url, token, deny_run, "waiting")
    deny_id = _approval_id(base_url, token, deny_run)
    stream_before_deny = _stream_length(env)
    deny_status, _ = _request(
        base_url,
        "POST",
        f"/api/v1/runs/{deny_run}/approvals/{deny_id}",
        token=token,
        body={"decision": "denied", "comment": "deny smoke"},
    )
    if deny_status != 200:
        raise RuntimeError(f"deny must close synchronously, got {deny_status}")
    _wait_run_status(base_url, token, deny_run, "failed")
    deny_state = inspect_run(env, deny_run)
    deny_evidence = postgres_approval_evidence(deny_state, expected_status="denied")
    deny_ack = run_queue_ack_evidence(
        env,
        stream=STREAM,
        group=GROUP,
        run_id=deny_run,
    )
    if _stream_length(env) != stream_before_deny:
        raise RuntimeError("deny created a continuation queue operation")
    if (Path(env["SERVICE_APP_SMOKE_DIR"]) / "workspace" / "denied.txt").exists():
        raise RuntimeError("deny executed the protected tool handler")

    return {
        "approval": {
            "run_id": approval_run,
            "checkpoint_id": approval_state["checkpoint_id"],
            "status": "approved",
            "enqueue_recovery": "ok",
            "ordered_evidence": approval_evidence,
            "worker_ack": approval_ack,
            "write_before_failure": {
                "tool_invocation": invocation_before_recovery,
                "resolution_event_count": 0,
            },
            "ack_before_failure": {
                "ordered_evidence": approval_evidence_before_ack,
                "redis": pending_before_ack,
                "hard_crash_exit": 24,
            },
        },
        "deny": {
            "run_id": deny_run,
            "status": "failed",
            "continuations": 0,
            "ordered_evidence": deny_evidence,
            "worker_ack": deny_ack,
        },
    }


__all__ = ["run_approval_smoke"]
