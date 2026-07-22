"""准备 service smoke 依赖、迁移、预算断言与认证边界。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from service_http_smoke import request
from service_secret_smoke import verify_secret_failure_cases
from service_smoke_support import (
    compose,
    compose_result,
    last_json_line,
    postgres_counts,
    stream_length,
)

STREAM = "agent-harness:service:runs:stream"


@dataclass(frozen=True)
class BootstrapEvidence:
    """依赖启动、迁移与预算合同完成后可跨阶段传递的非敏感证据。"""

    secret_failures: dict[str, object]
    budget_race: dict[str, Any]
    budget_topology: dict[str, Any]


def _admin_assertion(env: dict[str, str], *, boundary: str, command: str) -> dict[str, Any]:
    """运行 PostgreSQL 管理断言，并把稳定错误类型写入 smoke 边界。"""

    env["SERVICE_APP_SMOKE_BOUNDARY"] = boundary
    result = compose_result(
        env,
        "run",
        "--rm",
        "migration",
        "python",
        "scripts/service_admin.py",
        command,
    )
    if result.returncode != 0:
        try:
            diagnostic = last_json_line(result.stdout)
        except (RuntimeError, ValueError, json.JSONDecodeError):
            diagnostic = {}
        error_type = str(diagnostic.get("error_type") or "unknown")
        error_code = str(diagnostic.get("error_code") or "none")
        env["SERVICE_APP_SMOKE_BOUNDARY"] = f"{boundary}-{error_type}-{error_code}"
        raise RuntimeError(f"PostgreSQL {boundary} failed")
    return last_json_line(result.stdout)


def prepare_service(env: dict[str, str], token: str) -> BootstrapEvidence:
    """完成镜像、密钥失败合同、迁移、预算断言与 API 引导。"""

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "image-build"
    compose(env, "build", "migration")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "redis-readiness"
    compose(env, "up", "-d", "--wait", "postgres", "redis")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-failure-contracts"
    secret_failures = verify_secret_failure_cases(env)
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "migration"
    compose(env, "run", "--rm", "migration")
    budget_race = _admin_assertion(
        env,
        boundary="postgres-budget-race",
        command="assert-budget-race",
    )
    budget_topology = _admin_assertion(
        env,
        boundary="postgres-budget-topology",
        command="assert-budget-topology",
    )

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
    return BootstrapEvidence(
        secret_failures=cast(dict[str, object], secret_failures),
        budget_race=budget_race,
        budget_topology=budget_topology,
    )


def verify_authentication(env: dict[str, str], base_url: str) -> None:
    """证明缺失或错误凭据不会创建 run、audit 或队列副作用。"""

    before_counts = postgres_counts(env)
    before_stream = stream_length(env, STREAM)
    missing_status, _ = request(
        base_url,
        "POST",
        "/api/v1/agents/examples.basic/runs",
        body={"input": {}},
    )
    invalid_status, _ = request(
        base_url,
        "POST",
        "/api/v1/agents/examples.basic/runs",
        token="invalid-service-smoke-token",
        body={"input": {}},
    )
    if (missing_status, invalid_status) != (401, 401):
        raise RuntimeError("service verifier did not reject missing/invalid credential")
    if postgres_counts(env) != before_counts or stream_length(env, STREAM) != before_stream:
        raise RuntimeError("rejected credential created run, audit, or queue side effects")
