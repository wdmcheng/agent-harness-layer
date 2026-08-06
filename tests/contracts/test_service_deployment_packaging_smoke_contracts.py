"""服务容器打包、冒烟能力清单与密钥加载边界合同。"""

from __future__ import annotations

from typing import Any, cast

import yaml
from tests.contracts.service_deployment_test_support import ROOT, TEMPLATE, load_compose


def test_compose_declares_migration_api_worker_and_shared_runtime_configuration() -> None:
    """Compose 必须为迁移、API 和 worker 提供一致的密钥与运行时边界。"""

    payload = load_compose()
    services = payload["services"]
    assert {"postgres", "redis", "migration", "api", "worker"} <= services.keys()

    shared_keys = {
        "AGENT_HARNESS_STORAGE__DSN_FILE",
        "AGENT_HARNESS_QUEUE__DSN",
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE",
        "SERVICE_APP_EXECUTOR_ID",
        "SERVICE_APP_RECLAIM_IDLE_SECONDS",
    }
    # 三个进程共享同一密钥来源，但只能通过文件挂载读取，不能回退到明文环境变量。
    for name in ("migration", "api", "worker"):
        assert shared_keys <= services[name]["environment"].keys()
        assert services[name]["profiles"] == ["service"]
        assert services[name]["environment"]["AGENT_HARNESS_STORAGE__DSN_FILE"] == (
            "/run/secrets/agent_harness_storage_dsn"
        )
        assert services[name]["secrets"] == [
            "agent_harness_storage_dsn",
            "agent_harness_budget_fingerprint_key",
        ]
        assert "AGENT_HARNESS_STORAGE__DSN" not in services[name]["environment"]

    assert "agent-harness-service" in services["api"]["command"]
    assert "app.workers.runtime_worker" in services["worker"]["command"]
    assert "app.migrate" in services["migration"]["command"]
    assert payload["secrets"]["agent_harness_storage_dsn"]["file"] == (
        "${SERVICE_APP_STORAGE_DSN_FILE:?SERVICE_APP_STORAGE_DSN_FILE is required}"
    )
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/agent_harness_postgres_password"
    )
    assert "POSTGRES_PASSWORD" not in services["postgres"]["environment"]
    assert services["postgres"]["secrets"] == ["agent_harness_postgres_password"]
    assert payload["secrets"]["agent_harness_postgres_password"]["file"] == (
        "${SERVICE_APP_POSTGRES_PASSWORD_FILE:?SERVICE_APP_POSTGRES_PASSWORD_FILE is required}"
    )
    assert services["api"]["depends_on"]["migration"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["worker"]


def test_container_build_requires_core_wheel_and_does_not_copy_workspace_source() -> None:
    """镜像构建只能安装已产出的核心 wheel，避免把宿主工作区带入运行镜像。"""

    dockerfile = (TEMPLATE / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (TEMPLATE / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY .agent-harness/agent_harness-*.whl" in dockerfile
    assert "packages/agent-harness" not in dockerfile
    assert "PYTHONPATH" not in dockerfile
    assert "--no-deps ." in dockerfile
    assert "!.agent-harness/agent_harness-*.whl" in dockerignore
    assert ".env" in dockerignore


def test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup() -> None:
    """服务冒烟脚本需覆盖关键恢复链路，并将清理范围约束在当前项目。"""

    template_smoke = (TEMPLATE / "scripts" / "smoke_service.py").read_text(encoding="utf-8")
    scenario_smoke = "\n".join(
        (TEMPLATE / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "service_smoke_scenarios.py",
            "service_smoke_bootstrap.py",
            "service_smoke_reclaim.py",
            "service_smoke_evidence.py",
        )
    )
    secret_smoke = (TEMPLATE / "scripts" / "service_secret_smoke.py").read_text(encoding="utf-8")
    approval_smoke = (TEMPLATE / "scripts" / "service_approval_smoke.py").read_text(
        encoding="utf-8"
    )
    root_smoke = (ROOT / "scripts" / "smoke_service.py").read_text(encoding="utf-8")

    # 标记分布在运行、密钥和审批场景，组合后必须覆盖完整跨进程恢复边界。
    for marker in (
        "missing_status",
        "invalid_status",
        "XPENDING",
        "SERVICE_APP_SMOKE_CRASH_AFTER_OWNER",
        "hard_crash_exit",
        "checkpoint_id",
        "enqueue_recovery",
        "continuations",
        "stale_receipt_rejected",
        "worker-a-receipt.json",
        "worker-b-receipt.json",
        "reclaim-release",
        "SERVICE_APP_SMOKE_FAIL_AFTER_BOOTSTRAP",
        "SERVICE_APP_KEEP_DATA",
        '"postgresql"',
        "cleanup_project",
        "SERVICE_APP_STORAGE_DSN_FILE",
        "SERVICE_APP_POSTGRES_PASSWORD_FILE",
        "storage-dsn.secret",
        "verify_secret_failure_cases",
        '"credential-cleanup"',
        '"server-version-evidence"',
        '"trace-export"',
        '"symlink": True',
        '"conflict": True',
        '"postgres_password_file": True',
        '"compose_config_redacted": True',
    ):
        assert (
            marker in template_smoke
            or marker in scenario_smoke
            or marker in secret_smoke
            or marker in approval_smoke
        )
    for marker in (
        '"unreadable": True',
        '"api_worker_readiness_blocked": True',
        '"side_effects": False',
        "AGENT_HARNESS_STORAGE__DSN_FILE=/run/secrets/symlink-fixture/storage-dsn-link",
        ":/run/secrets/symlink-fixture:ro",
        "assert_configuration_secret_absent",
        'compose(env, "config")',
        '"compose-config"',
        "path not in {secret_path, postgres_password_path}",
        '"doctor"',
        '"pg_dump"',
    ):
        assert marker in secret_smoke
    assert "TemporaryDirectory" in root_smoke
    for boundary in (
        '"checkpoint-approval-submit"',
        '"checkpoint-approval-waiting"',
        '"checkpoint-approval-id"',
        '"checkpoint-approval-outage"',
        '"checkpoint-approval-redis-ready"',
    ):
        assert boundary in approval_smoke
    assert "copytree" in root_smoke
    assert "wheel.name" in root_smoke
    assert "secret-cleanup=ok" in root_smoke
    assert "docker system prune" not in template_smoke
    assert "secret_path.unlink" in template_smoke
    support = (TEMPLATE / "scripts" / "service_smoke_support.py").read_text(encoding="utf-8")
    postgres_evidence = (TEMPLATE / "scripts" / "service_postgres_evidence.py").read_text(
        encoding="utf-8"
    )
    assert '"terminal_event"' in postgres_evidence
    assert '"docker", "network", "inspect", network' in support
    assert '"docker", "volume", "inspect", volume' in support


def test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios() -> None:
    """真实 service producer 必须执行 PostgreSQL 迁移、并发预算与崩溃恢复场景。"""

    smoke = "\n".join(
        (TEMPLATE / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "smoke_service.py",
            "service_smoke_scenarios.py",
            "service_smoke_bootstrap.py",
            "service_smoke_reclaim.py",
            "service_smoke_evidence.py",
        )
    )

    for marker in (
        'compose(env, "run", "--rm", "migration")',
        '"assert-budget-race"',
        '"assert-budget-topology"',
        '"shared-budget-crash-windows"',
        '"0018_model_tool_loop_state"',
    ):
        assert marker in smoke


def test_service_profile_and_admin_do_not_bypass_typed_secret_loader() -> None:
    """服务配置和管理脚本必须复用类型化密钥加载器，不能直接读取敏感环境变量。"""

    profile = cast(
        dict[str, Any],
        yaml.safe_load(
            (TEMPLATE / "configs" / "profiles" / "service.yaml").read_text(encoding="utf-8")
        ),
    )
    admin = (TEMPLATE / "scripts" / "service_admin.py").read_text(encoding="utf-8")
    env_example = (TEMPLATE / ".env.example").read_text(encoding="utf-8")

    assert profile["storage"]["dsn"] is None
    assert "load_settings" in admin
    assert "storage_dsn_from_settings" in admin
    assert 'os.environ.get("AGENT_HARNESS_STORAGE__DSN"' not in admin
    assert "SERVICE_APP_STORAGE_DSN_FILE=.agent-harness/secrets/storage-dsn" in env_example
    assert (
        "SERVICE_APP_POSTGRES_PASSWORD_FILE=.agent-harness/secrets/postgres-password" in env_example
    )
    assert "rm -f .agent-harness/secrets/storage-dsn" in env_example
