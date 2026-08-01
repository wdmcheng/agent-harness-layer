"""以隔离 Compose project 验证 HTTP、Redis、DBOS、PostgreSQL 与审批恢复。"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from uuid import uuid4

from service_secret_smoke import (
    verify_secret_failure_cases,
)
from service_smoke_operations import (
    parse_args,
    prepare_core_wheel,
)
from service_smoke_scenarios import run_service_smoke
from service_smoke_support import (
    cleanup_credential_at_boundary,
    cleanup_project,
    compose,
    failure_diagnostic,
    free_port,
    preserve_postgres_volume,
    run,
)

APP_ROOT = Path(__file__).resolve().parents[1]
STREAM = "agent-harness:service:runs:stream"
GROUP = "agent-harness-workers"


def _server_versions(env: dict[str, str]) -> dict[str, str]:
    """从运行中的容器读取实际 server version，避免用策略版本冒充 smoke 事实。"""

    postgres_output = compose(env, "exec", "-T", "postgres", "postgres", "--version")
    redis_output = compose(env, "exec", "-T", "redis", "redis-server", "--version")
    postgres_match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", postgres_output)
    redis_match = re.search(r"Redis server v=?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", redis_output)
    if postgres_match is None or redis_match is None:
        raise RuntimeError("service smoke did not report database server versions")
    return {"postgres": postgres_match.group(1), "redis": redis_match.group(1)}


def main() -> int:
    """创建一次性隔离环境，运行 smoke，并在所有退出路径清除临时凭据与资源。

    即使中途失败，也先尝试删除 bootstrap credential，再依据显式保留条件决定
    是否保留 PostgreSQL volume；所有 token、DSN 与预算指纹文件均以受限权限
    写入，finally 中必须删除，防止本地调试残留泄露到下一次运行。
    """
    args = parse_args()
    prepare_core_wheel()
    project = os.environ.get("SERVICE_APP_COMPOSE_PROJECT") or f"agent-harness-{uuid4().hex[:10]}"
    smoke_dir = APP_ROOT / ".agent-harness" / project
    database_password = secrets.token_urlsafe(24)
    secret_path = smoke_dir / "storage-dsn.secret"
    postgres_password_path = smoke_dir / "postgres-password.secret"
    budget_fingerprint_path = smoke_dir / "budget-fingerprint.secret"
    token = secrets.token_urlsafe(32)
    tenant_id = f"smoke-{uuid4()}"
    env = {
        **os.environ,
        "SERVICE_APP_COMPOSE_PROJECT": project,
        "SERVICE_APP_API_PORT": str(free_port()),
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_STORAGE_DSN_FILE": str(secret_path),
        "SERVICE_APP_POSTGRES_PASSWORD_FILE": str(postgres_password_path),
        "SERVICE_APP_BUDGET_FINGERPRINT_KEY_FILE": str(budget_fingerprint_path),
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
    approval_write_worker = f"{project}-approval-write-fail"
    approval_ack_worker = f"{project}-approval-ack-fail"
    trace_export = APP_ROOT / ".agent-harness/service-smoke-trace.jsonl"
    trace_export.unlink(missing_ok=True)
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
        budget_fingerprint_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        budget_fingerprint_path.chmod(0o600)
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
                "migration": "0017_model_route_chain_state",
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
            evidence = run_service_smoke(env, token, tenant_id)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "credential-cleanup"
            if not cleanup_credential_at_boundary(env, token):
                raise RuntimeError("service smoke credential cleanup did not delete one record")
            credential_cleanup_confirmed = True
            credential_cleanup_needed = False
            evidence["credential_cleanup"] = {"deleted": 1}
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "server-version-evidence"
            evidence["server_versions"] = _server_versions(env)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "trace-export"
            trace_source = smoke_dir / "trace.jsonl"
            if not trace_source.is_file() or trace_source.stat().st_size == 0:
                raise RuntimeError("service smoke runtime trace is missing")
            trace_export.parent.mkdir(parents=True, exist_ok=True)
            temporary_trace = trace_export.with_name(f".{trace_export.name}.{project}.tmp")
            try:
                shutil.copyfile(trace_source, temporary_trace)
                temporary_trace.replace(trace_export)
            finally:
                temporary_trace.unlink(missing_ok=True)
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
                run(
                    [
                        "docker",
                        "rm",
                        "-f",
                        worker_a,
                        approval_write_worker,
                        approval_ack_worker,
                    ],
                    env=env,
                    check=False,
                )
            finally:
                try:
                    cleanup_project(env, preserve_volume=preserve_volume)
                finally:
                    secret_path.unlink(missing_ok=True)
                    postgres_password_path.unlink(missing_ok=True)
                    budget_fingerprint_path.unlink(missing_ok=True)
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
