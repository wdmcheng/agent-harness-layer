"""Service 凭据清理、reclaim 与 admin 证据合同测试。"""

from __future__ import annotations

from types import SimpleNamespace

from tests.contracts.service_deployment_test_support import (
    TEMPLATE as TEMPLATE,
)
from tests.contracts.service_deployment_test_support import (
    EvidenceOperationKind as EvidenceOperationKind,
)
from tests.contracts.service_deployment_test_support import (
    Path as Path,
)
from tests.contracts.service_deployment_test_support import (
    RunCreate as RunCreate,
)
from tests.contracts.service_deployment_test_support import (
    SessionCreate as SessionCreate,
)
from tests.contracts.service_deployment_test_support import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.service_deployment_test_support import (
    isolated_database as isolated_database,
)
from tests.contracts.service_deployment_test_support import (
    load_service_admin as load_service_admin,
)
from tests.contracts.service_deployment_test_support import (
    load_smoke_support as load_smoke_support,
)
from tests.contracts.service_deployment_test_support import (
    os as os,
)
from tests.contracts.service_deployment_test_support import (
    pytest as pytest,
)
from tests.contracts.service_deployment_test_support import (
    run_migrations as run_migrations,
)
from tests.contracts.service_deployment_test_support import (
    seed_persisted_run as seed_persisted_run,
)


def test_service_budget_race_uses_repository_accepted_frozen_snapshot_identity() -> None:
    """真实 service 探针不得用自定义 snapshot 标签绕过正式版本 validator。"""

    source = (TEMPLATE / "scripts" / "service_admin_budget_race.py").read_text(encoding="utf-8")

    assert 'snapshot_id = f"budget-tree-v1:' in source
    assert 'snapshot_id = f"budget-race-snapshot-' not in source


def test_service_budget_topology_uses_repository_accepted_frozen_snapshot_identity() -> None:
    """共享预算 topology 探针也必须使用仓储认可的冻结快照身份。"""

    source = (TEMPLATE / "scripts" / "service_admin_budget_topology.py").read_text(encoding="utf-8")

    assert 'snapshot_id = f"budget-tree-v1:' in source
    assert 'snapshot_id = f"budget-topology-{label}-' not in source


def test_failure_diagnostic_omits_raw_secret_path_and_provider_error() -> None:
    """运维失败诊断必须保留边界定位信息，同时剔除 DSN 密码、令牌、路径及供应商原始错误。"""

    support = load_smoke_support()
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
    """保留 PostgreSQL 卷只能在凭据已确认清理时启用，避免残留数据与访问令牌同时存在。"""

    support = load_smoke_support()

    assert support.preserve_postgres_volume(True, credential_cleanup_confirmed=True) is True
    assert support.preserve_postgres_volume(True, credential_cleanup_confirmed=False) is False
    assert support.preserve_postgres_volume(False, credential_cleanup_confirmed=True) is False


def test_compose_command_accepts_runtime_identity_only_from_internal_override(
    tmp_path: Path,
) -> None:
    """基础命令保持固定身份，wrapper 生成的 override 才能加入 Compose 文件链。"""

    support = load_smoke_support()
    env = {"SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123"}

    base_command = support._compose_command(env, "config")
    assert base_command.count("-f") == 1

    env["SERVICE_APP_RUNTIME_USER_OVERRIDE_CONTENT"] = "services: {}\n"
    override_command = support._compose_command(env, "config")

    assert override_command[:7] == [
        "docker",
        "compose",
        "-f",
        str(support.COMPOSE_FILE),
        "-f",
        "-",
        "-p",
    ]


def test_cleanup_project_falls_back_to_labels_when_smoke_identity_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径身份异常不能阻断 project label 级容器、网络和卷清理。"""

    support = load_smoke_support()
    smoke_dir = tmp_path / "safe-project"
    smoke_dir.mkdir()
    created = smoke_dir.stat()
    smoke_dir.rename(tmp_path / "orphan")
    smoke_dir.mkdir()
    commands: list[list[str]] = []

    def record_run(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool = True,
        input_text: str | None = None,
    ) -> SimpleNamespace:
        del env, check, input_text
        commands.append(command)
        is_query = (
            command[:3] == ["docker", "ps", "-aq"]
            or command[:3] == ["docker", "network", "ls"]
            or command[:3] == ["docker", "volume", "ls"]
        )
        return SimpleNamespace(stdout="", returncode=0 if is_query else 1)

    monkeypatch.setattr(support, "run", record_run)
    support.cleanup_project(
        {
            "SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123",
            "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
            "SERVICE_APP_SMOKE_DEVICE": str(created.st_dev),
            "SERVICE_APP_SMOKE_INODE": str(created.st_ino),
        },
        preserve_volume=False,
    )

    assert any("label=com.docker.compose.project=agent-harness-safe123" in row for row in commands)
    assert any(row[:3] == ["docker", "network", "rm"] for row in commands)
    assert any(row[:3] == ["docker", "volume", "rm"] for row in commands)


def test_cleanup_project_rejects_repeated_docker_query_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daemon/transport 查询持续失败不能被解释为项目资源已不存在。"""

    support = load_smoke_support()
    commands: list[list[str]] = []

    def fail_docker_queries(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool = True,
        input_text: str | None = None,
    ) -> SimpleNamespace:
        del env, check, input_text
        commands.append(command)
        return SimpleNamespace(stdout="", returncode=1)

    def skip_retry_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(support, "run", fail_docker_queries)
    monkeypatch.setattr(support.time, "sleep", skip_retry_delay)

    with pytest.raises(RuntimeError, match="boundary=project-cleanup"):
        support.cleanup_project(
            {"SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123"},
            preserve_volume=False,
        )

    assert sum(row[:3] == ["docker", "ps", "-aq"] for row in commands) >= 20


def test_cleanup_project_removes_and_proves_labeled_resources_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """label fallback 必须删除真实存在的三类资源，再由成功查询证明为空。"""

    support = load_smoke_support()
    project = "agent-harness-safe123"
    network = f"{project}_default"
    volume = f"{project}_agent_harness_postgres_data"
    resources = {"container": True, "network": True, "volume": True}
    commands: list[list[str]] = []

    def stateful_run(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool = True,
        input_text: str | None = None,
    ) -> SimpleNamespace:
        del env, check, input_text
        commands.append(command)
        if command[:3] == ["docker", "ps", "-aq"]:
            return SimpleNamespace(
                stdout="container-id\n" if resources["container"] else "",
                returncode=0,
            )
        if command[:3] == ["docker", "rm", "-f"]:
            resources["container"] = False
        elif command[:3] == ["docker", "network", "rm"]:
            assert command[3] == network
            resources["network"] = False
        elif command[:3] == ["docker", "volume", "rm"]:
            assert command[3] == volume
            resources["volume"] = False
        elif command[:3] == ["docker", "network", "ls"]:
            return SimpleNamespace(
                stdout=f"{network}\n" if resources["network"] else "",
                returncode=0,
            )
        elif command[:3] == ["docker", "volume", "ls"]:
            return SimpleNamespace(
                stdout=f"{volume}\n" if resources["volume"] else "",
                returncode=0,
            )
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(support, "run", stateful_run)

    support.cleanup_project(
        {"SERVICE_APP_COMPOSE_PROJECT": project},
        preserve_volume=False,
    )

    assert resources == {"container": False, "network": False, "volume": False}
    assert any(row[:3] == ["docker", "network", "ls"] for row in commands)
    assert any(row[:3] == ["docker", "volume", "ls"] for row in commands)


def test_cleanup_project_passes_frozen_override_content_to_compose_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带 `-f -` 的 down 必须同步传入本轮冻结的 stdin override。"""

    support = load_smoke_support()
    calls: list[tuple[list[str], str | None]] = []

    def record_run(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool = True,
        input_text: str | None = None,
    ) -> SimpleNamespace:
        del env, check
        calls.append((command, input_text))
        is_query = (
            command[:3] == ["docker", "ps", "-aq"]
            or command[:3] == ["docker", "network", "ls"]
            or command[:3] == ["docker", "volume", "ls"]
        )
        return SimpleNamespace(stdout="", returncode=0 if is_query else 1)

    monkeypatch.setattr(support, "run", record_run)
    override = "services: {}\n"
    support.cleanup_project(
        {
            "SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123",
            "SERVICE_APP_RUNTIME_USER_OVERRIDE_CONTENT": override,
        },
        preserve_volume=False,
    )

    down_calls = [call for call in calls if "down" in call[0]]
    assert down_calls
    assert down_calls[0][1] == override


def test_failed_credential_cleanup_routes_to_redacted_cleanup_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据清理失败要切换到可脱敏的 cleanup 边界，诊断中不能回显失败操作携带的秘密。"""

    support = load_smoke_support()
    env = {"SERVICE_APP_COMPOSE_PROJECT": "agent-harness-safe123"}

    def failed_cleanup(
        _env: dict[str, str],
        _token: str,
        *,
        check: bool = True,
    ) -> bool:
        """模拟外部清理动作失败，保留调用形状以验证失败分支而不执行真实凭据操作。"""

        del check
        return False

    monkeypatch.setattr(support, "cleanup_credential", failed_cleanup)

    assert support.cleanup_credential_at_boundary(env, "secret-smoke-token") is False

    diagnostic = support.failure_diagnostic(
        env["SERVICE_APP_SMOKE_BOUNDARY"],
        env,
        raw_detail="postgresql://agent:plain-password@postgres/db /Users/private",
    )
    assert "boundary=credential-cleanup" in diagnostic
    assert "secret-smoke-token" not in diagnostic
    assert "plain-password" not in diagnostic
    assert "/Users/private" not in diagnostic


def test_reclaim_receipts_require_two_real_owners_and_delivery_increment() -> None:
    """重领证据必须证明消息从一个真实 worker 交给另一个 worker，且 delivery count 已递增。"""

    support = load_smoke_support()
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
    """终态 PostgreSQL 证据应关联请求、工作流、用量 outbox、容量和共享预算，而不臆造缺失字段。"""

    support = load_smoke_support()
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

    admin = load_service_admin()
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

        admin = load_service_admin()
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
    """提交的 service 配置只能引用受控密钥文件与容器网络，不能固化本地应用 DSN 或开发令牌。"""

    profile = (TEMPLATE / "configs" / "profiles" / "service.yaml").read_text(encoding="utf-8")
    compose = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")

    assert "localhost:55432" not in profile
    assert "localhost:56379" in profile
    assert "postgres:5432" not in compose
    assert "AGENT_HARNESS_STORAGE__DSN_FILE" in compose
    assert "redis:6379" in compose
    assert "dev_bearer_token" not in profile
