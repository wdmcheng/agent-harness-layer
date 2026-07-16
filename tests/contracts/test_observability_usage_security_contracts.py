"""规范用量 fanout、幂等与大载荷安全合同测试。"""

from __future__ import annotations

from tests.contracts.test_observability_provider_adapters_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    FileArtifactStore as FileArtifactStore,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    OTelTelemetryAdapter as OTelTelemetryAdapter,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    Path as Path,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    RecordingProviderAdapter as RecordingProviderAdapter,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    StorageRunTraceResolver as StorageRunTraceResolver,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    TelemetryContext as TelemetryContext,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    TelemetryFacade as TelemetryFacade,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    TelemetryRecord as TelemetryRecord,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    json as json,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    pytest as pytest,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    seed_persisted_run as seed_persisted_run,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    usage_payload as usage_payload,
)


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
