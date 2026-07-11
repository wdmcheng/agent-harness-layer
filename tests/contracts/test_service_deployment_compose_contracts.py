"""四服务 Compose、wheel-only 镜像与隔离 smoke 的静态部署合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

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
        "AGENT_HARNESS_STORAGE__DSN",
        "AGENT_HARNESS_QUEUE__DSN",
        "SERVICE_APP_EXECUTOR_ID",
        "SERVICE_APP_RECLAIM_IDLE_SECONDS",
    }
    for name in ("migration", "api", "worker"):
        assert shared_keys <= services[name]["environment"].keys()
        assert services[name]["profiles"] == ["service"]

    assert "agent-harness-service" in services["api"]["command"]
    assert "app.workers.runtime_worker" in services["worker"]["command"]
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
    ):
        assert marker in template_smoke
    assert "TemporaryDirectory" in root_smoke
    assert "copytree" in root_smoke
    assert "wheel.name" in root_smoke
    assert "docker system prune" not in template_smoke
    support = (TEMPLATE / "scripts" / "service_smoke_support.py").read_text(encoding="utf-8")
    assert '"terminal_event"' in support
    assert '"docker", "network", "inspect", network' in support
    assert '"docker", "volume", "inspect", volume' in support


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
        "trace_id": None,
        "events": [
            {
                "event_id": "event-1",
                "type": "run.completed",
                "terminal": True,
                "request_id": "request-1",
                "trace_id": None,
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


def test_service_profile_keeps_container_overrides_out_of_committed_dsn() -> None:
    profile = (TEMPLATE / "configs" / "profiles" / "service.yaml").read_text(encoding="utf-8")
    compose = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")

    assert "localhost:55432" in profile
    assert "localhost:56379" in profile
    assert "postgres:5432" in compose
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
