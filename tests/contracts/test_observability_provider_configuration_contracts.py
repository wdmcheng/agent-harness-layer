"""可观测性 provider 适配、配置与降级诊断合同测试。"""

from __future__ import annotations

from tests.contracts.test_observability_provider_adapters_contracts import (
    PROFILES as PROFILES,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    Any as Any,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    Path as Path,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    TelemetryContext as TelemetryContext,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    TelemetryRecord as TelemetryRecord,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    json as json,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    load_settings as load_settings,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    redact_telemetry_payload as redact_telemetry_payload,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    subprocess as subprocess,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    sys as sys,
)


def test_provider_adapters_accept_fake_clients_and_keep_payload_provider_neutral() -> None:
    """Logfire/Phoenix/Langfuse contract tests 不需要真实账号，只锁本仓库边界。"""

    from agent_harness.adapters.observability.langfuse import LangfuseTelemetryAdapter
    from agent_harness.adapters.observability.logfire import LogfireTelemetryAdapter
    from agent_harness.adapters.observability.phoenix import PhoenixTelemetryAdapter

    sent: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        """记录各 provider 接收到的规范化 payload，避免接入真实 SaaS 客户端。"""

        def send(self, provider: str, payload: dict[str, Any]) -> None:
            """保存 provider 名与 payload，供统一脱敏和 provider-neutral 断言使用。"""

            sent.append((provider, payload))

    record = TelemetryRecord(
        name="agent_harness.run.completed",
        record_type="event",
        context=TelemetryContext(tenant_id="default", run_id="run-1", trace_id="trace-1"),
        payload={
            "status": "completed",
            "authorization": "Bearer secret-token-12345",
            "large_payload": "provider-payload-" * 600,
        },
    )

    statuses = [
        LogfireTelemetryAdapter(client=FakeClient()).send_sync(record),
        PhoenixTelemetryAdapter(client=FakeClient()).send_sync(record),
        LangfuseTelemetryAdapter(client=FakeClient()).send_sync(record),
    ]

    assert [status.status for status in statuses] == ["sent", "sent", "sent"]
    assert {provider for provider, _payload in sent} == {"logfire", "phoenix", "langfuse"}
    serialized = json.dumps([payload for _provider, payload in sent])
    assert "secret-token-12345" not in serialized
    assert "provider-payload-" not in serialized
    assert "provider" in sent[0][1]
    assert "context" in sent[0][1]
    assert sent[0][1]["payload_ref"].startswith("payload://sha256/")


def test_redact_telemetry_payload_covers_eval_audit_error_and_header_shapes() -> None:
    """同一个脱敏入口覆盖 trace、eval-like、audit-like 和错误摘要。"""

    redacted = redact_telemetry_payload(
        {
            "headers": {"Authorization": "Bearer raw-token-12345"},
            "eval": {"expected": "api_key=eval-secret-12345"},
            "audit": {"cookie": "session-cookie"},
            "error": (
                "provider failed with Authorization: Bearer error-auth-12345; "
                "Cookie: sessionid=error-cookie-12345; token=error-secret-12345"
            ),
        }
    )

    serialized = json.dumps(redacted)
    assert "raw-token-12345" not in serialized
    assert "eval-secret-12345" not in serialized
    assert "session-cookie" not in serialized
    assert "error-auth-12345" not in serialized
    assert "error-cookie-12345" not in serialized
    assert "error-secret-12345" not in serialized
    assert redacted["headers"]["Authorization"] == "[REDACTED]"


def test_config_profiles_expose_observability_provider_boundary() -> None:
    """profile 只声明 provider 边界；加载配置不启动 provider 或要求真实 key。"""

    local = load_settings(profile="local", profiles_dir=PROFILES)
    service = load_settings(profile="service", profiles_dir=PROFILES)

    assert local.observability.kind == "local-jsonl"
    assert local.observability.providers == []
    assert service.observability.kind == "local-jsonl"
    assert {provider.kind for provider in service.observability.providers} == {
        "otel",
        "logfire",
        "phoenix",
        "langfuse",
    }
    assert all(provider.enabled is False for provider in service.observability.providers)


def test_doctor_reports_observability_provider_degradation_without_secret(
    tmp_path: Path,
) -> None:
    """doctor 要报告 provider 配置状态，但不能要求 SaaS key 或泄漏 token。"""

    db_path = tmp_path / "doctor.db"
    run_migrations(sqlite_dsn(db_path))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            sqlite_dsn(db_path),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "observability sink: local-jsonl writable" in result.stdout
    assert "observability provider: none configured" in result.stdout
    assert "token" not in result.stdout.lower()
