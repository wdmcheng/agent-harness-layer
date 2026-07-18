"""Service 凭据清理、reclaim 与 admin 证据合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_deployment_compose_contracts import (
    TEMPLATE as TEMPLATE,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    EvidenceOperationKind as EvidenceOperationKind,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    Path as Path,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    SessionCreate as SessionCreate,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    _service_admin as _service_admin,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    _smoke_support as _smoke_support,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    isolated_database as isolated_database,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    os as os,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    pytest as pytest,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    seed_persisted_run as seed_persisted_run,
)


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
                "event_id": "usage-started",
                "type": "model.request.started",
                "seq": 1,
                "terminal": False,
                "visibility": "internal",
                "request_id": "request-1",
                "trace_id": "trace-1",
                "payload": {"correlation": {"usage_call_id": "usage-1"}},
            },
            {
                "event_id": "usage-final",
                "type": "model.usage.updated",
                "seq": 2,
                "terminal": False,
                "visibility": "internal",
                "request_id": "request-1",
                "trace_id": "trace-1",
                "payload": {
                    "correlation": {"usage_call_id": "usage-1"},
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "event_id": "event-1",
                "type": "run.completed",
                "seq": 3,
                "terminal": True,
                "visibility": "public",
                "request_id": "request-1",
                "trace_id": "trace-1",
                "payload": None,
            },
        ],
        "outbox": [
            {
                "event_id": "usage-final",
                "usage_call_id": "usage-1",
                "operation_kind": "model_usage",
                "state": "published",
            }
        ],
        "capacity": {
            "highest_persisted_seq": 3,
            "outstanding_reserved_event_count": 0,
            "terminal_reservation": 0,
        },
        "shared_budget": {
            "owner_run_id": "run-1",
            "token_limit": 100,
            "cost_enabled": False,
            "token_impact": 15,
            "cost_impact": "0E-8",
            "state": "terminal",
            "claims": [
                {
                    "operation_kind": "direct",
                    "usage_call_id": "usage-1",
                    "state": "settled",
                    "side_effect_state": "result_committed",
                    "token_impact": 15,
                }
            ],
            "allocations": [],
        },
    }

    evidence = support.postgres_terminal_evidence(
        expected,
        completed,
        workflow_id="workflow-1",
    )

    assert evidence["execution"] == expected
    assert evidence["terminal_event"]["request_id"] == "request-1"
    assert evidence["terminal_event"]["trace_id"] == "trace-1"
    assert evidence["usage"]["usage_call_id"] == "usage-1"
    assert evidence["usage"]["outbox_state"] == "published"


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


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL service admin 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_service_admin_inspect_run_reads_postgresql_capacity_and_outbox() -> None:
    """inspect seam 必须能读取 0014 PostgreSQL 空 outbox 与初始容量。"""

    async with isolated_database("service_admin_inspect") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            async with storage.uow() as uow:
                await uow.tenants.ensure("inspect-pg")
                session = await uow.sessions.create(
                    SessionCreate(
                        tenant_id="inspect-pg",
                        user_id="user-pg",
                        agent_id="examples.basic",
                    )
                )
                run = await uow.runs.create(
                    RunCreate(
                        tenant_id="inspect-pg",
                        session_id=session.id,
                        agent_id="examples.basic",
                        trace_id="trace-inspect-pg",
                    )
                )
                await uow.commit()
            run_id = run.id
            async with storage.uow() as uow:
                reserved = await uow.event_capacity.reserve(
                    run_id=run_id,
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                )
                await uow.evidence_outbox.start_usage(
                    tenant_id="inspect-pg",
                    run_id=run_id,
                    usage_call_id="usage-inspect-pg",
                    event_id="usage:inspect-pg:usage-inspect-pg:final",
                    reserved_event_count=reserved,
                    started_evidence={
                        "usage_kind": "model",
                        "tenant_id": "inspect-pg",
                        "provider": "fake",
                        "model": "fake-basic",
                        "input_tokens": None,
                        "output_tokens": None,
                        "cost_usd": None,
                        "cost_status": "unavailable",
                        "latency_ms": 0,
                        "decision": {"provider_called": False},
                        "run_id": run_id,
                        "agent_id": "examples.basic",
                        "request_id": None,
                        "trace_id": "trace-inspect-pg",
                    },
                )
                await uow.commit()
        finally:
            await storage.dispose()

        admin = _service_admin()
        admin.storage_dsn = lambda: dsn
        inspected = await admin.inspect_run(run_id)

        assert inspected["outbox"] == [
            {
                "event_id": "usage:inspect-pg:usage-inspect-pg:final",
                "usage_call_id": "usage-inspect-pg",
                "operation_kind": "model_usage",
                "state": "started",
                "reserved_event_count": 2,
                "group_id": None,
                "sequence_in_group": None,
            }
        ]
        assert inspected["capacity"] == {
            "highest_persisted_seq": 0,
            "outstanding_reserved_event_count": 2,
            "terminal_reservation": 1,
        }


def test_service_profile_keeps_application_dsn_out_of_committed_config() -> None:
    profile = (TEMPLATE / "configs" / "profiles" / "service.yaml").read_text(encoding="utf-8")
    compose = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")

    assert "localhost:55432" not in profile
    assert "localhost:56379" in profile
    assert "postgres:5432" not in compose
    assert "AGENT_HARNESS_STORAGE__DSN_FILE" in compose
    assert "redis:6379" in compose
    assert "dev_bearer_token" not in profile
