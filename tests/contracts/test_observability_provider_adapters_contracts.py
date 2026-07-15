"""Observability provider adapter 的公开契约测试。

这些测试只穿过稳定公共 seam：TelemetryFacade、trace context DTO、
provider adapter contract、typed config 和 doctor CLI。它们不要求真实 SaaS 账号，
也不声明 eval case / score workflow 已经完成。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.artifacts import FileArtifactStore
from agent_harness.config import load_settings
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.observability import (
    OTelTelemetryAdapter,
    ProviderTelemetryAdapter,
    TelemetryContext,
    TelemetryFacade,
    TelemetryRecord,
    TelemetryStatus,
    redact_telemetry_payload,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


class RecordingProviderAdapter(ProviderTelemetryAdapter):
    """测试用 provider adapter，记录 facade fan-out 前收到的脱敏 DTO。"""

    provider_name = "recording"

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.records: list[TelemetryRecord] = []
        self.fail_with = fail_with

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        self.records.append(record)
        if self.fail_with is not None:
            raise self.fail_with
        return TelemetryStatus(provider=self.provider_name, status="sent")


def usage_payload(*, run_id: str, trace_id: str) -> dict[str, object]:
    """构造 Facade canonical usage 合同使用的完整统一 DTO。"""

    return {
        "usage_kind": "model",
        "tenant_id": "default",
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 3,
        "decision": {"provider_called": True},
        "run_id": run_id,
        "agent_id": "agent-1",
        "request_id": None,
        "trace_id": trace_id,
    }


@pytest.mark.asyncio
async def test_non_run_telemetry_facade_preserves_nullable_trace(tmp_path: Path) -> None:
    """没有真实 run 归属的 telemetry 不生成假的 canonical trace。"""

    events_path = tmp_path / "non-run-telemetry.jsonl"
    facade = TelemetryFacade(local_sink=LocalJsonlEventSink(events_path))
    result = await facade.publish_record(
        TelemetryRecord(
            name="agent_harness.audit.policy",
            record_type="event",
            context=TelemetryContext(tenant_id="default"),
            payload={"decision": "deny"},
        )
    )

    persisted = json.loads(events_path.read_text(encoding="utf-8"))
    assert result.local_status.status == "written"
    assert persisted["record_scope"] == "non_run"
    assert "trace_id" not in persisted
    assert "trace_id" not in persisted["payload"]["telemetry"]["context"]


@pytest.mark.asyncio
async def test_telemetry_facade_writes_local_first_and_fans_out_redacted_provider_payload(
    tmp_path: Path,
) -> None:
    """local/jsonl 是长期证据；provider 只收到 provider-neutral 且已脱敏的 DTO。"""

    events_path = tmp_path / "telemetry.jsonl"
    dsn = sqlite_dsn(tmp_path / "telemetry.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-1")
    resolver = StorageRunTraceResolver(storage)
    event_bus = EventBus(
        sink=LocalJsonlEventSink(events_path, run_trace_resolver=resolver),
        run_trace_resolver=resolver,
    )
    provider = RecordingProviderAdapter()
    facade = TelemetryFacade(
        local_sink=LocalJsonlEventSink(events_path, run_trace_resolver=resolver),
        providers=[provider],
    )
    try:
        source_event = await event_bus.publish(
            tenant_id="default",
            user_id="user-1",
            agent_id="agent-1",
            run_id=run_id,
            event_type=CanonicalEventType.TOOL_CALL_COMPLETED,
            payload={
                "tool_name": "shell.execute",
                "api_key": "sk-abcdef1234567890",
                "stdout": "token=provider-secret-12345",
            },
            trace_id="trace-1",
            span_id="span-1",
        )
        result = await facade.publish_event(source_event)
    finally:
        await storage.dispose()

    assert result.local_status.status == "written"
    assert [status.status for status in result.provider_statuses] == ["sent"]
    persisted = events_path.read_text(encoding="utf-8")
    assert "sk-abcdef1234567890" not in persisted
    assert "provider-secret-12345" not in persisted
    assert provider.records[0].context.trace_id == "trace-1"
    assert provider.records[0].context.span_id == "span-1"
    assert provider.records[0].payload["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_provider_failure_is_degraded_and_does_not_drop_local_evidence(
    tmp_path: Path,
) -> None:
    """外部 provider 异常不能反向破坏 local trace/audit evidence。"""

    events_path = tmp_path / "telemetry.jsonl"
    dsn = sqlite_dsn(tmp_path / "telemetry.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-1")
    provider = RecordingProviderAdapter(
        fail_with=RuntimeError(
            "provider Authorization: Bearer leaked-secret-12345; "
            "Cookie: sessionid=raw-cookie-12345 failed"
        )
    )
    facade = TelemetryFacade(
        local_sink=LocalJsonlEventSink(
            events_path,
            run_trace_resolver=StorageRunTraceResolver(storage),
        ),
        providers=[provider],
    )
    record = TelemetryRecord(
        name="agent_harness.audit.policy",
        record_type="event",
        context=TelemetryContext(
            tenant_id="default",
            user_id="user-1",
            agent_id="agent-1",
            run_id=run_id,
            trace_id="trace-1",
        ),
        payload={"decision": "deny", "password": "p@ss"},
    )

    try:
        result = await facade.publish_record(record)
    finally:
        await storage.dispose()

    assert result.local_status.status == "written"
    assert result.provider_statuses[0].provider == "recording"
    assert result.provider_statuses[0].status == "degraded"
    assert result.provider_statuses[0].detail is not None
    assert "leaked-secret-12345" not in result.provider_statuses[0].detail
    assert "raw-cookie-12345" not in result.provider_statuses[0].detail
    persisted = events_path.read_text(encoding="utf-8")
    assert "p@ss" not in persisted
    assert "leaked-secret-12345" not in persisted
    assert "raw-cookie-12345" not in persisted


@pytest.mark.asyncio
async def test_canonical_usage_is_written_once_then_only_fanned_out(tmp_path: Path) -> None:
    """usage 的 local writer 只有 EventBus，Facade 不追加第二条 CanonicalEvent。"""

    dsn = sqlite_dsn(tmp_path / "usage-telemetry.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-usage")
    resolver = StorageRunTraceResolver(storage)
    sink = LocalJsonlEventSink(tmp_path / "usage.jsonl", run_trace_resolver=resolver)
    provider = RecordingProviderAdapter()
    event_bus = EventBus(sink=sink, run_trace_resolver=resolver)
    facade = TelemetryFacade(local_sink=sink, providers=[provider])
    try:
        usage = await event_bus.publish(
            tenant_id="default",
            run_id=run_id,
            agent_id="agent-1",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            payload={
                "correlation": {"usage_call_id": "usage-a"},
                "usage": usage_payload(run_id=run_id, trace_id="trace-usage"),
            },
            trace_id="trace-usage",
            event_id="usage:default:usage-a:final",
        )
        result = await facade.publish_event(usage)
        persisted = await sink.read(run_id=run_id)
    finally:
        await storage.dispose()

    assert result.local_status.status == "already_written"
    assert [status.status for status in result.provider_statuses] == ["sent"]
    assert len(persisted) == 1
    assert provider.records[0].payload["correlation"]["usage_call_id"] == "usage-a"


@pytest.mark.asyncio
async def test_canonical_usage_without_provider_keeps_one_local_event(tmp_path: Path) -> None:
    """未配置 SaaS provider 时，usage local evidence 仍唯一且 provider 结果为空。"""

    dsn = sqlite_dsn(tmp_path / "usage-no-provider.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-usage-none")
    resolver = StorageRunTraceResolver(storage)
    sink = LocalJsonlEventSink(tmp_path / "usage-no-provider.jsonl", run_trace_resolver=resolver)
    event_bus = EventBus(sink=sink, run_trace_resolver=resolver)
    facade = TelemetryFacade(local_sink=sink)
    try:
        usage = await event_bus.publish(
            tenant_id="default",
            run_id=run_id,
            agent_id="agent-1",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            payload={
                "correlation": {"usage_call_id": "usage-none"},
                "usage": usage_payload(run_id=run_id, trace_id="trace-usage-none"),
            },
            trace_id="trace-usage-none",
            event_id="usage:default:usage-none:final",
        )
        result = await facade.publish_event(usage)
        persisted = await sink.read(run_id=run_id)
    finally:
        await storage.dispose()

    assert result.local_status.status == "already_written"
    assert result.provider_statuses == []
    assert [event.event_id for event in persisted] == ["usage:default:usage-none:final"]


@pytest.mark.asyncio
async def test_canonical_usage_provider_failure_is_degraded_and_redacted(tmp_path: Path) -> None:
    """usage provider 失败不能删除 local evidence，失败摘要也不能泄漏 secret。"""

    dsn = sqlite_dsn(tmp_path / "usage-provider-failure.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-usage-degraded")
    resolver = StorageRunTraceResolver(storage)
    sink = LocalJsonlEventSink(
        tmp_path / "usage-provider-failure.jsonl",
        run_trace_resolver=resolver,
    )
    provider = RecordingProviderAdapter(
        fail_with=RuntimeError("Authorization: Bearer provider-secret-12345")
    )
    event_bus = EventBus(sink=sink, run_trace_resolver=resolver)
    facade = TelemetryFacade(local_sink=sink, providers=[provider])
    try:
        usage = await event_bus.publish(
            tenant_id="default",
            run_id=run_id,
            agent_id="agent-1",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            payload={
                "correlation": {"usage_call_id": "usage-degraded"},
                "usage": usage_payload(run_id=run_id, trace_id="trace-usage-degraded"),
            },
            trace_id="trace-usage-degraded",
            event_id="usage:default:usage-degraded:final",
        )
        result = await facade.publish_event(usage)
        persisted = await sink.read(run_id=run_id)
    finally:
        await storage.dispose()

    assert len(persisted) == 1
    assert len(provider.records) == 1
    assert result.provider_statuses[0].status == "degraded"
    assert result.provider_statuses[0].detail is not None
    assert "provider-secret-12345" not in result.provider_statuses[0].detail


@pytest.mark.asyncio
async def test_canonical_usage_local_write_failure_prevents_provider_fanout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EventBus durable write 失败时，provider fan-out 必须保持零副作用。"""

    dsn = sqlite_dsn(tmp_path / "usage-local-failure.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-usage-failure")
    resolver = StorageRunTraceResolver(storage)
    sink = LocalJsonlEventSink(tmp_path / "usage-local-failure.jsonl", run_trace_resolver=resolver)
    provider = RecordingProviderAdapter()
    event_bus = EventBus(sink=sink, run_trace_resolver=resolver)
    facade = TelemetryFacade(local_sink=sink, providers=[provider])

    async def fail_write(_event: object) -> None:
        raise OSError("durable sink unavailable")

    monkeypatch.setattr(sink, "write", fail_write)
    try:
        with pytest.raises(OSError, match="durable sink unavailable"):
            usage = await event_bus.publish(
                tenant_id="default",
                run_id=run_id,
                agent_id="agent-1",
                event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
                payload={
                    "correlation": {"usage_call_id": "usage-local-failure"},
                    "usage": usage_payload(
                        run_id=run_id,
                        trace_id="trace-usage-failure",
                    ),
                },
                trace_id="trace-usage-failure",
                event_id="usage:default:usage-local-failure:final",
            )
            await facade.publish_event(usage)
    finally:
        await storage.dispose()

    assert provider.records == []


@pytest.mark.asyncio
async def test_canonical_usage_fanout_rejects_same_id_with_different_envelope(
    tmp_path: Path,
) -> None:
    """相同 event_id 不能授权伪造 envelope 复用已持久化身份。"""

    dsn = sqlite_dsn(tmp_path / "usage-envelope-mismatch.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-envelope")
    resolver = StorageRunTraceResolver(storage)
    sink = LocalJsonlEventSink(tmp_path / "usage-envelope.jsonl", run_trace_resolver=resolver)
    event_bus = EventBus(sink=sink, run_trace_resolver=resolver)
    provider = RecordingProviderAdapter()
    facade = TelemetryFacade(local_sink=sink, providers=[provider])
    try:
        persisted = await event_bus.publish(
            tenant_id="default",
            run_id=run_id,
            agent_id="agent-1",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            payload={
                "correlation": {"usage_call_id": "usage-envelope"},
                "usage": usage_payload(run_id=run_id, trace_id="trace-envelope"),
                "outcome": "completed",
            },
            trace_id="trace-envelope",
            event_id="usage:default:usage-envelope:final",
        )
        assert persisted.payload is not None
        forged = persisted.model_copy(
            update={"payload": {**persisted.payload, "outcome": "forged"}}
        )
        with pytest.raises(ValueError, match="canonical usage"):
            await facade.publish_event(forged)
    finally:
        await storage.dispose()

    assert provider.records == []


@pytest.mark.asyncio
async def test_canonical_usage_direct_facade_publish_is_rejected(tmp_path: Path) -> None:
    dsn = sqlite_dsn(tmp_path / "usage-direct.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-usage")
    resolver = StorageRunTraceResolver(storage)
    sink = LocalJsonlEventSink(tmp_path / "usage-direct.jsonl", run_trace_resolver=resolver)
    event_bus = EventBus(sink=sink, run_trace_resolver=resolver)
    provider = RecordingProviderAdapter()
    facade = TelemetryFacade(local_sink=sink, providers=[provider])
    try:
        event = await event_bus.publish(
            tenant_id="default",
            run_id=run_id,
            agent_id="agent-1",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            payload={"placeholder": True},
            trace_id="trace-usage",
        )
        unpersisted_usage = event.model_copy(
            update={
                "event_id": "usage:not-persisted",
                "event_type": CanonicalEventType.MODEL_USAGE_UPDATED,
            }
        )
        with pytest.raises(ValueError, match="EventBus-persisted"):
            await facade.publish_event(unpersisted_usage)
    finally:
        await storage.dispose()
    assert provider.records == []


@pytest.mark.asyncio
async def test_large_payload_is_written_as_artifact_ref_before_local_or_provider_fanout(
    tmp_path: Path,
) -> None:
    """大 payload 只能以 artifact/ref 进入 trace，不内联塞进 local/provider。"""

    events_path = tmp_path / "telemetry.jsonl"
    dsn = sqlite_dsn(tmp_path / "telemetry.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await seed_persisted_run(storage, trace_id="trace-large")
    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    provider = RecordingProviderAdapter()
    facade = TelemetryFacade(
        local_sink=LocalJsonlEventSink(
            events_path,
            run_trace_resolver=StorageRunTraceResolver(storage),
        ),
        providers=[provider],
        artifact_store=artifact_store,
        inline_payload_bytes=128,
    )
    large_output = "tool-output-" * 80
    record = TelemetryRecord(
        name="agent_harness.tool.call.completed",
        record_type="event",
        context=TelemetryContext(
            tenant_id="default",
            run_id=run_id,
            trace_id="trace-large",
        ),
        payload={
            "stdout": large_output,
            "authorization": "Bearer raw-auth-token-12345",
        },
    )

    try:
        result = await facade.publish_record(record)
    finally:
        await storage.dispose()

    assert result.local_status.status == "written"
    persisted = events_path.read_text(encoding="utf-8")
    assert large_output not in persisted
    assert "raw-auth-token-12345" not in persisted
    persisted_event = json.loads(persisted)
    telemetry_payload = persisted_event["payload"]["telemetry"]
    assert telemetry_payload["payload_ref"].startswith("artifact://")
    assert "artifact" in telemetry_payload["payload"]
    assert provider.records[0].payload_ref == telemetry_payload["payload_ref"]
    provider_payload = json.dumps(provider.records[0].to_payload())
    assert large_output not in provider_payload
    assert "raw-auth-token-12345" not in provider_payload
    artifact_payload = artifact_store.read_json(provider.records[0].payload_ref or "")
    assert artifact_payload["stdout"] == large_output
    assert artifact_payload["authorization"] == "[REDACTED]"


def test_telemetry_context_merges_applicable_correlation_fields() -> None:
    """不同事件类型追加自己的字段，不伪造不适用的关联。"""

    base = TelemetryContext(
        tenant_id="default",
        user_id="user-1",
        agent_id="agent-1",
        run_id="run-1",
        session_id="session-1",
        trace_id="trace-1",
        span_id="span-1",
    )

    tool_context = base.with_fields(tool_name="shell.execute", request_id="req-1")
    model_context = base.with_fields(model_provider="fake", model_name="fake-local")
    eval_context = base.with_fields(eval_run_id="eval-run-1")

    assert tool_context.tool_name == "shell.execute"
    assert tool_context.request_id == "req-1"
    assert tool_context.model_provider is None
    assert model_context.model_provider == "fake"
    assert model_context.model_name == "fake-local"
    assert eval_context.eval_run_id == "eval-run-1"


def test_otel_adapter_outputs_span_metric_and_event_without_raw_secret() -> None:
    """OTel adapter contract 覆盖 Product-Spec 要求的 span/metric/event 三类输出。"""

    adapter = OTelTelemetryAdapter()
    record = TelemetryRecord(
        name="agent_harness.model.usage.updated",
        record_type="metric",
        context=TelemetryContext(
            tenant_id="default",
            user_id="user-1",
            agent_id="agent-1",
            run_id="run-1",
            trace_id="trace-1",
            span_id="span-1",
            model_provider="fake",
            model_name="fake-local",
        ),
        payload={
            "duration_ms": 42,
            "input_tokens": 12,
            "output_tokens": 7,
            "raw_output": "large-model-output-" * 600,
            "api_key": "sk-abcdef1234567890",
        },
    )

    mapped = adapter.map_record(record)

    assert mapped.span.name == "agent_harness.model.usage.updated"
    assert mapped.event.name == "agent_harness.model.usage.updated"
    assert mapped.span.attributes["trace_id"] == "trace-1"
    assert mapped.span.attributes["model_provider"] == "fake"
    assert {metric.name for metric in mapped.metrics} == {
        "agent_harness.duration_ms",
        "agent_harness.input_tokens",
        "agent_harness.output_tokens",
    }
    serialized = json.dumps(mapped.to_payload())
    assert "sk-abcdef1234567890" not in serialized
    assert "large-model-output-" not in serialized
    assert mapped.span.attributes["payload_ref"].startswith("payload://sha256/")


def test_provider_adapters_accept_fake_clients_and_keep_payload_provider_neutral() -> None:
    """Logfire/Phoenix/Langfuse contract tests 不需要真实账号，只锁本仓库边界。"""

    from agent_harness.adapters.observability.langfuse import LangfuseTelemetryAdapter
    from agent_harness.adapters.observability.logfire import LogfireTelemetryAdapter
    from agent_harness.adapters.observability.phoenix import PhoenixTelemetryAdapter

    sent: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def send(self, provider: str, payload: dict[str, Any]) -> None:
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
