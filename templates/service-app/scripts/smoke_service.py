"""以隔离 Compose project 验证真实 HTTP、Redis、DBOS、PostgreSQL 与审批恢复。"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from service_http_smoke import (
    approval_id as _approval_id,
)
from service_http_smoke import (
    request as _request,
)
from service_http_smoke import (
    submit as _submit,
)
from service_http_smoke import (
    wait_for as _wait_for,
)
from service_http_smoke import (
    wait_run_status as _wait_run_status,
)
from service_secret_smoke import (
    assert_configuration_secret_absent,
    verify_secret_failure_cases,
)
from service_smoke_support import (
    assert_stale_receipt,
    cleanup_credential_at_boundary,
    cleanup_project,
    compose,
    failure_diagnostic,
    first_stream_message,
    free_port,
    inspect_run,
    last_json_line,
    parse_args,
    postgres_counts,
    postgres_terminal_evidence,
    prepare_core_wheel,
    preserve_postgres_volume,
    reclaim_receipts_match,
    redis_json,
    run,
    stream_length,
)

APP_ROOT = Path(__file__).resolve().parents[1]
STREAM = "agent-harness:service:runs:stream"
GROUP = "agent-harness-workers"


def _stream_length(env: dict[str, str]) -> int:
    return stream_length(env, STREAM)


def _first_message(env: dict[str, str]) -> tuple[str, dict[str, Any]]:
    return first_stream_message(env, STREAM)


def _run_smoke(env: dict[str, str], token: str, tenant_id: str) -> dict[str, object]:
    base_url = f"http://127.0.0.1:{env['SERVICE_APP_API_PORT']}"
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "image-build"
    compose(env, "build", "migration")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "redis-readiness"
    compose(env, "up", "-d", "--wait", "postgres", "redis")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-failure-contracts"
    secret_failures = verify_secret_failure_cases(env)
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "migration"
    compose(env, "run", "--rm", "migration")

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "credential-bootstrap"
    bootstrap_env = {**env, "SERVICE_APP_BOOTSTRAP_TOKEN": token}
    last_json_line(
        compose(
            bootstrap_env,
            "run",
            "--rm",
            "-e",
            "SERVICE_APP_BOOTSTRAP_TOKEN",
            "-e",
            "SERVICE_APP_BOOTSTRAP_TENANT",
            "migration",
            "python",
            "scripts/service_admin.py",
            "bootstrap",
        )
    )
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "api-readiness"
    compose(env, "up", "-d", "--wait", "api")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "api-auth"
    if env.get("SERVICE_APP_SMOKE_FAIL_AFTER_BOOTSTRAP") == "1":
        raise RuntimeError("deterministic smoke failure after credential bootstrap")

    before_counts = postgres_counts(env)
    before_stream = _stream_length(env)
    missing_status, _ = _request(
        base_url,
        "POST",
        "/api/v1/agents/examples.basic/runs",
        body={"input": {}},
    )
    invalid_status, _ = _request(
        base_url,
        "POST",
        "/api/v1/agents/examples.basic/runs",
        token="invalid-service-smoke-token",
        body={"input": {}},
    )
    if (missing_status, invalid_status) != (401, 401):
        raise RuntimeError("service verifier did not reject missing/invalid credential")
    if postgres_counts(env) != before_counts or _stream_length(env) != before_stream:
        raise RuntimeError("rejected credential created run, audit, or queue side effects")

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "pickup-reclaim"
    request_id = f"request-{uuid4()}"
    idempotency_key = f"smoke-{uuid4()}"
    submitted = _submit(
        base_url,
        token,
        agent_id="examples.basic",
        input_payload={"source_ref": "source://compose-smoke", "trust_level": "trusted"},
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    run_id = cast(str, submitted["run_id"])
    message_id, message = _first_message(env)
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
        result = run(
            ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}", worker_a],
            env=env,
            check=False,
        )
        return result.stdout.strip() == "exited|23"

    _wait_for("worker A hard crash", crashed)
    marker = json.loads((Path(env["SERVICE_APP_SMOKE_DIR"]) / "crash-owner.json").read_text())
    worker_a_receipt = json.loads(
        (Path(env["SERVICE_APP_SMOKE_DIR"]) / "worker-a-receipt.json").read_text()
    )
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
    worker_b_receipt_path = Path(env["SERVICE_APP_SMOKE_DIR"]) / "worker-b-receipt.json"
    _wait_for("worker B reclaim receipt", worker_b_receipt_path.exists)
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
    (Path(env["SERVICE_APP_SMOKE_DIR"]) / "reclaim-release").touch()
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "dbos-event"
    _wait_run_status(base_url, token, run_id, "completed")
    completed = inspect_run(env, run_id)
    postgres_evidence = postgres_terminal_evidence(
        execution_expected,
        completed,
        workflow_id=marker["workflow_id"],
    )
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "idempotency-replay"
    replay = _submit(
        base_url,
        token,
        agent_id="examples.basic",
        input_payload={"source_ref": "source://compose-smoke", "trust_level": "trusted"},
        idempotency_key=idempotency_key,
        request_id=f"retry-{uuid4()}",
    )
    if replay["run_id"] != run_id:
        raise RuntimeError("idempotent HTTP retry created another run")

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "checkpoint-approval"
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
    _wait_run_status(base_url, token, approval_run, "waiting")
    approval_id = _approval_id(base_url, token, approval_run)
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
    _wait_for(
        "Redis restart", lambda: compose(env, "exec", "-T", "redis", "redis-cli", "PING") == "PONG"
    )
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
    if _stream_length(env) != stream_before_deny:
        raise RuntimeError("deny created a continuation queue operation")
    if (Path(env["SERVICE_APP_SMOKE_DIR"]) / "workspace" / "denied.txt").exists():
        raise RuntimeError("deny executed the protected tool handler")

    evidence: dict[str, object] = {
        "migration": "0013a_run_trace_event_hardening",
        "secret_file": {
            "consumers": ["migration", "api", "worker"],
            "postgres_password_file": True,
            "compose_config_redacted": True,
            "redacted": True,
            "failure_cases": secret_failures,
        },
        "auth": {"missing": 401, "invalid": 401, "side_effects": 0},
        "queue": {
            **expected,
            "message_id": message_id,
            "delivery_count": worker_b_receipt["delivery_count"],
            "stale_receipt_rejected": True,
        },
        "dbos": {
            "executor_id": marker["executor_id"],
            "owner_id": marker["owner_id"],
            "workflow_id": marker["workflow_id"],
            "hard_crash_exit": 23,
        },
        "run": {"run_id": run_id, "status": "completed", "terminal_count": 1},
        "postgresql": postgres_evidence,
        "approval": {
            "run_id": approval_run,
            "checkpoint_id": approval_state["checkpoint_id"],
            "status": "approved",
            "enqueue_recovery": "ok",
        },
        "deny": {"run_id": deny_run, "status": "failed", "continuations": 0},
    }
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-scan"
    assert_configuration_secret_absent(
        env,
        base_url=base_url,
        evidence=evidence,
        request=_request,
    )
    return evidence


def main() -> int:
    args = parse_args()
    prepare_core_wheel()
    project = os.environ.get("SERVICE_APP_COMPOSE_PROJECT") or f"agent-harness-{uuid4().hex[:10]}"
    smoke_dir = APP_ROOT / ".agent-harness" / project
    database_password = secrets.token_urlsafe(24)
    secret_path = smoke_dir / "storage-dsn.secret"
    postgres_password_path = smoke_dir / "postgres-password.secret"
    token = secrets.token_urlsafe(32)
    tenant_id = f"smoke-{uuid4()}"
    env = {
        **os.environ,
        "SERVICE_APP_COMPOSE_PROJECT": project,
        "SERVICE_APP_API_PORT": str(free_port()),
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_STORAGE_DSN_FILE": str(secret_path),
        "SERVICE_APP_POSTGRES_PASSWORD_FILE": str(postgres_password_path),
        "SERVICE_APP_BOOTSTRAP_TENANT": tenant_id,
        "SERVICE_APP_RECLAIM_IDLE_SECONDS": os.environ.get("SERVICE_APP_RECLAIM_IDLE_SECONDS", "1"),
        "SERVICE_APP_IMAGE": f"agent-harness-service-app:{project}",
        "SERVICE_APP_SMOKE_BOUNDARY": "startup",
    }
    os.environ["SERVICE_APP_SMOKE_DIR"] = str(smoke_dir)
    keep_data = os.environ.get("SERVICE_APP_KEEP_DATA") == "1"
    credential_cleanup_confirmed = args.migrate_only
    credential_cleanup_needed = False
    worker_a = f"{project}-worker-a"
    try:
        smoke_dir.mkdir(parents=True, exist_ok=True)
        (smoke_dir / "workspace").mkdir(exist_ok=True)
        (smoke_dir / "artifacts").mkdir(exist_ok=True)
        secret_path.write_text(
            f"postgresql+asyncpg://agent_harness:{database_password}@postgres:5432/agent_harness",
            encoding="utf-8",
        )
        secret_path.chmod(0o600)
        postgres_password_path.write_text(database_password, encoding="utf-8")
        postgres_password_path.chmod(0o600)
        if args.migrate_only:
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "image-build"
            compose(env, "build", "migration")
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "postgres-readiness"
            compose(env, "up", "-d", "--wait", "postgres")
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-failure-contracts"
            secret_failures = verify_secret_failure_cases(env)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "migration"
            compose(env, "run", "--rm", "migration")
            evidence: dict[str, object] = {
                "migration": "0013a_run_trace_event_hardening",
                "secret_file": {
                    "consumers": ["migration"],
                    "postgres_password_file": True,
                    "compose_config_redacted": True,
                    "redacted": True,
                    "failure_cases": secret_failures,
                },
            }
        else:
            credential_cleanup_needed = True
            evidence = _run_smoke(env, token, tenant_id)
            if not cleanup_credential_at_boundary(env, token):
                raise RuntimeError("service smoke credential cleanup did not delete one record")
            credential_cleanup_confirmed = True
            credential_cleanup_needed = False
            evidence["credential_cleanup"] = {"deleted": 1}
        print("smoke-service: " + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        print("smoke-service: ok")
        return 0
    except Exception:
        compose(env, "logs", "--no-color", "--tail", "120", "api", "worker", check=False)
        diagnostic = failure_diagnostic(env["SERVICE_APP_SMOKE_BOUNDARY"], env)
        raise RuntimeError(diagnostic) from None
    finally:
        try:
            if credential_cleanup_needed and not credential_cleanup_confirmed:
                credential_cleanup_confirmed = cleanup_credential_at_boundary(
                    env,
                    token,
                    check=False,
                )
        finally:
            preserve_volume = preserve_postgres_volume(
                keep_data,
                credential_cleanup_confirmed=credential_cleanup_confirmed,
            )
            try:
                run(["docker", "rm", "-f", worker_a], env=env, check=False)
            finally:
                try:
                    cleanup_project(env, preserve_volume=preserve_volume)
                finally:
                    secret_path.unlink(missing_ok=True)
                    postgres_password_path.unlink(missing_ok=True)
                    shutil.rmtree(smoke_dir, ignore_errors=True)
                    os.environ.pop("SERVICE_APP_SMOKE_DIR", None)
                    for wheel in (APP_ROOT / ".agent-harness").glob("agent_harness-*.whl"):
                        wheel.unlink(missing_ok=True)
        if preserve_volume:
            volume = f"{project}_agent_harness_postgres_data"
            inspected = run(["docker", "volume", "inspect", volume], env=env, check=False)
            if inspected.returncode == 0:
                print(f"smoke-service: kept volume={volume}")
                print(f"smoke-service: cleanup=docker volume rm {volume}")


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        print(str(exc))
        exit_code = 1
    raise SystemExit(exit_code)
