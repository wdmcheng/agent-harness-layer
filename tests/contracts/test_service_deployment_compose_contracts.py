"""四服务 Compose、wheel-only 镜像与隔离 smoke 的静态部署合同。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.auth import ApiKeyVerifier, hash_token
from agent_harness.storage import ApiKeyCreate, SQLAlchemyStorage, run_migrations

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "service-app"


def _smoke_support() -> Any:
    path = TEMPLATE / "scripts" / "service_smoke_support.py"
    spec = importlib.util.spec_from_file_location("service_smoke_support_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    support = _smoke_support()
    monkeypatch.setitem(sys.modules, "service_smoke_support", support)
    http_path = TEMPLATE / "scripts" / "service_http_smoke.py"
    http_spec = importlib.util.spec_from_file_location(
        "service_http_smoke_contract",
        http_path,
    )
    assert http_spec is not None and http_spec.loader is not None
    http_module = importlib.util.module_from_spec(http_spec)
    http_spec.loader.exec_module(http_module)
    monkeypatch.setitem(sys.modules, "service_http_smoke", http_module)
    secret_path = TEMPLATE / "scripts" / "service_secret_smoke.py"
    secret_spec = importlib.util.spec_from_file_location(
        "service_secret_smoke_contract",
        secret_path,
    )
    assert secret_spec is not None and secret_spec.loader is not None
    secret_module = importlib.util.module_from_spec(secret_spec)
    secret_spec.loader.exec_module(secret_module)
    monkeypatch.setitem(sys.modules, "service_secret_smoke", secret_module)
    path = TEMPLATE / "scripts" / "smoke_service.py"
    spec = importlib.util.spec_from_file_location("service_smoke_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _service_admin() -> Any:
    path = TEMPLATE / "scripts" / "service_admin.py"
    spec = importlib.util.spec_from_file_location("service_admin_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root_smoke() -> Any:
    path = ROOT / "scripts" / "smoke_service.py"
    spec = importlib.util.spec_from_file_location("root_service_smoke_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")),
    )


def test_compose_declares_migration_api_worker_and_shared_runtime_configuration() -> None:
    payload = _compose()
    services = payload["services"]
    assert {"postgres", "redis", "migration", "api", "worker"} <= services.keys()

    shared_keys = {
        "AGENT_HARNESS_STORAGE__DSN_FILE",
        "AGENT_HARNESS_QUEUE__DSN",
        "SERVICE_APP_EXECUTOR_ID",
        "SERVICE_APP_RECLAIM_IDLE_SECONDS",
    }
    for name in ("migration", "api", "worker"):
        assert shared_keys <= services[name]["environment"].keys()
        assert services[name]["profiles"] == ["service"]
        assert services[name]["environment"]["AGENT_HARNESS_STORAGE__DSN_FILE"] == (
            "/run/secrets/agent_harness_storage_dsn"
        )
        assert services[name]["secrets"] == ["agent_harness_storage_dsn"]
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
    dockerfile = (TEMPLATE / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (TEMPLATE / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY .agent-harness/agent_harness-*.whl" in dockerfile
    assert "packages/agent-harness" not in dockerfile
    assert "PYTHONPATH" not in dockerfile
    assert "--no-deps ." in dockerfile
    assert "!.agent-harness/agent_harness-*.whl" in dockerignore
    assert ".env" in dockerignore


def test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup() -> None:
    template_smoke = (TEMPLATE / "scripts" / "smoke_service.py").read_text(encoding="utf-8")
    secret_smoke = (TEMPLATE / "scripts" / "service_secret_smoke.py").read_text(encoding="utf-8")
    root_smoke = (ROOT / "scripts" / "smoke_service.py").read_text(encoding="utf-8")

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
        '"symlink": True',
        '"conflict": True',
        '"postgres_password_file": True',
        '"compose_config_redacted": True',
    ):
        assert marker in template_smoke or marker in secret_smoke
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
    assert "copytree" in root_smoke
    assert "wheel.name" in root_smoke
    assert "secret-cleanup=ok" in root_smoke
    assert "docker system prune" not in template_smoke
    assert "secret_path.unlink" in template_smoke
    support = (TEMPLATE / "scripts" / "service_smoke_support.py").read_text(encoding="utf-8")
    assert '"terminal_event"' in support
    assert '"docker", "network", "inspect", network' in support
    assert '"docker", "volume", "inspect", volume' in support


@pytest.mark.parametrize("failure", [RuntimeError("smoke failed"), KeyboardInterrupt()])
def test_service_smoke_cleans_secret_after_failure_or_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    smoke = _smoke_service(monkeypatch)
    project = "agent-harness-cleanup-contract"
    cleanup_calls: list[tuple[str, bool]] = []

    def fail_smoke(*_args: object) -> dict[str, object]:
        raise failure

    def no_compose_output(*_args: object, **_kwargs: object) -> str:
        return ""

    def cleanup_credential(*_args: object, **_kwargs: object) -> bool:
        return True

    def record_cleanup(env: dict[str, str], *, preserve_volume: bool) -> None:
        cleanup_calls.append((env["SERVICE_APP_COMPOSE_PROJECT"], preserve_volume))

    def ignored_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1)

    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: None)
    monkeypatch.setattr(smoke, "free_port", lambda: 43123)
    monkeypatch.setattr(smoke, "_run_smoke", fail_smoke)
    monkeypatch.setattr(smoke, "compose", no_compose_output)
    monkeypatch.setattr(smoke, "cleanup_credential_at_boundary", cleanup_credential)
    monkeypatch.setattr(smoke, "cleanup_project", record_cleanup)
    monkeypatch.setattr(smoke, "run", ignored_command)

    with pytest.raises(type(failure)):
        smoke.main()

    smoke_dir = tmp_path / ".agent-harness" / project
    assert not smoke_dir.exists()
    assert cleanup_calls == [(project, False)]


def test_service_smoke_deletes_secret_when_project_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_service(monkeypatch)
    project = "agent-harness-cleanup-failure"

    def fail_smoke(*_args: object) -> dict[str, object]:
        raise RuntimeError("smoke failed")

    def fail_cleanup(_env: dict[str, str], *, preserve_volume: bool) -> None:
        del preserve_volume
        raise RuntimeError("project cleanup failed")

    def no_compose_output(*_args: object, **_kwargs: object) -> str:
        return ""

    def cleanup_credential(*_args: object, **_kwargs: object) -> bool:
        return True

    def ignored_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1)

    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: None)
    monkeypatch.setattr(smoke, "free_port", lambda: 43124)
    monkeypatch.setattr(smoke, "_run_smoke", fail_smoke)
    monkeypatch.setattr(smoke, "compose", no_compose_output)
    monkeypatch.setattr(smoke, "cleanup_credential_at_boundary", cleanup_credential)
    monkeypatch.setattr(smoke, "cleanup_project", fail_cleanup)
    monkeypatch.setattr(smoke, "run", ignored_command)

    with pytest.raises(RuntimeError, match="project cleanup failed"):
        smoke.main()

    assert not (tmp_path / ".agent-harness" / project).exists()
    assert "SERVICE_APP_SMOKE_DIR" not in os.environ


def test_root_smoke_forwards_interrupt_to_child_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_smoke = _root_smoke()
    sent_signals: list[tuple[int, int]] = []

    class InterruptedProcess:
        pid = 43125

        def __init__(self) -> None:
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                assert timeout is None
                raise KeyboardInterrupt
            assert timeout == 30
            return 0

    process = InterruptedProcess()

    def fake_popen(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        start_new_session: bool,
    ) -> InterruptedProcess:
        assert cwd == tmp_path
        assert env["AGENT_HARNESS_SOURCE"] == str(tmp_path / "core.whl")
        assert start_new_session is True
        return process

    def record_signal(process_group: int, sent_signal: int) -> None:
        sent_signals.append((process_group, sent_signal))

    monkeypatch.setattr(root_smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(root_smoke.os, "killpg", record_signal)

    with pytest.raises(KeyboardInterrupt):
        root_smoke._run_copied_smoke(
            ["make", "smoke-service"],
            tmp_path,
            tmp_path / "core.whl",
        )

    assert sent_signals == [(process.pid, root_smoke.signal.SIGINT)]


def test_service_profile_and_admin_do_not_bypass_typed_secret_loader() -> None:
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


def test_failure_diagnostic_omits_raw_secret_path_and_provider_error() -> None:
    support = _smoke_support()
    raw = (
        "postgresql://agent:plain-password@postgres/db "
        "token=secret-smoke-token /Users/example/private provider raw failure"
    )
    diagnostic = support.failure_diagnostic(
        "api-worker",
        {"SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123"},
        raw_detail=raw,
    )

    assert "boundary=api-worker" in diagnostic
    assert "project=agent-harness-safe123" in diagnostic
    for secret in (
        "plain-password",
        "secret-smoke-token",
        "/Users/example/private",
        "provider raw",
    ):
        assert secret not in diagnostic

    migration = support.failure_diagnostic(
        "migration",
        {"SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123"},
        raw_detail=raw,
    )
    assert "boundary=migration" in migration
    assert migration != diagnostic


def test_keep_data_requires_confirmed_credential_cleanup() -> None:
    support = _smoke_support()

    assert support.preserve_postgres_volume(True, credential_cleanup_confirmed=True) is True
    assert support.preserve_postgres_volume(True, credential_cleanup_confirmed=False) is False
    assert support.preserve_postgres_volume(False, credential_cleanup_confirmed=True) is False


def test_failed_credential_cleanup_routes_to_redacted_cleanup_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _smoke_support()
    env = {"SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123"}

    def failed_cleanup(
        _env: dict[str, str],
        _token: str,
        *,
        check: bool = True,
    ) -> bool:
        del check
        return False

    monkeypatch.setattr(support, "cleanup_credential", failed_cleanup)

    assert support.cleanup_credential_at_boundary(env, "secret-smoke-token") is False

    diagnostic = support.failure_diagnostic(
        env["SERVICE_APP_SMOKE_BOUNDARY"],
        env,
        raw_detail="postgresql://agent:plain-password@postgres/db /Users/private",
    )
    assert "boundary=cleanup" in diagnostic
    assert "secret-smoke-token" not in diagnostic
    assert "plain-password" not in diagnostic
    assert "/Users/private" not in diagnostic


def test_reclaim_receipts_require_two_real_owners_and_delivery_increment() -> None:
    support = _smoke_support()
    worker_a = {
        "stream": "agent-harness:service:runs:stream",
        "group": "agent-harness-workers",
        "message_id": "1-0",
        "consumer_id": "worker-a",
        "delivery_count": 1,
    }
    worker_b = {**worker_a, "consumer_id": "worker-b", "delivery_count": 2}

    assert support.reclaim_receipts_match("1-0", worker_a, worker_b) is True
    assert (
        support.reclaim_receipts_match("1-0", worker_a, {**worker_b, "delivery_count": 1}) is False
    )


def test_postgres_terminal_evidence_correlates_applicable_fields() -> None:
    support = _smoke_support()
    expected = {
        "request_id": "request-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "run_id": "run-1",
        "message_id": "1-0",
    }
    completed = {
        **expected,
        "workflow_id": "workflow-1",
        "trace_id": "trace-1",
        "events": [
            {
                "event_id": "event-1",
                "type": "run.completed",
                "terminal": True,
                "request_id": "request-1",
                "trace_id": "trace-1",
            }
        ],
    }

    evidence = support.postgres_terminal_evidence(
        expected,
        completed,
        workflow_id="workflow-1",
    )

    assert evidence["execution"] == expected
    assert evidence["terminal_event"]["request_id"] == "request-1"
    assert evidence["terminal_event"]["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_service_admin_inspect_run_returns_persisted_trace(tmp_path: Path) -> None:
    """service smoke 的 DBOS evidence 读取器必须返回 run canonical trace。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'inspect-run.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-inspect")
    finally:
        await storage.dispose()

    admin = _service_admin()
    admin.storage_dsn = lambda: dsn
    inspected = await admin.inspect_run(run_id)

    assert inspected["run_id"] == run_id
    assert inspected["trace_id"] == "trace-inspect"


def test_service_profile_keeps_application_dsn_out_of_committed_config() -> None:
    profile = (TEMPLATE / "configs" / "profiles" / "service.yaml").read_text(encoding="utf-8")
    compose = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")

    assert "localhost:55432" not in profile
    assert "localhost:56379" in profile
    assert "postgres:5432" not in compose
    assert "AGENT_HARNESS_STORAGE__DSN_FILE" in compose
    assert "redis:6379" in compose
    assert "dev_bearer_token" not in profile


@pytest.mark.asyncio
async def test_api_key_identity_session_id_fits_persistent_session_contract(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'api-key.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("service-tenant")
            api_key = await uow.api_keys.create(
                ApiKeyCreate(
                    tenant_id="service-tenant",
                    user_id="service-user",
                    name="service-key",
                    token_hash=hash_token("service-token"),
                    roles=["operator"],
                    permissions=["runs:execute"],
                )
            )
            await uow.commit()
        identity = await ApiKeyVerifier(storage).verify("service-token")
    finally:
        await storage.dispose()

    assert identity is not None
    assert identity.session_id == api_key.id
    assert len(identity.session_id) <= 36


@pytest.mark.asyncio
async def test_ephemeral_api_key_can_be_deleted_by_hash(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'api-key-cleanup.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    token_hash = hash_token("ephemeral-service-token")
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("service-tenant")
            await uow.api_keys.create(
                ApiKeyCreate(
                    tenant_id="service-tenant",
                    user_id="service-user",
                    name="ephemeral-key",
                    token_hash=token_hash,
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            assert await uow.api_keys.delete_by_hash(token_hash) is True
            assert await uow.api_keys.delete_by_hash(token_hash) is False
            await uow.commit()
        assert await ApiKeyVerifier(storage).verify("ephemeral-service-token") is None
    finally:
        await storage.dispose()
