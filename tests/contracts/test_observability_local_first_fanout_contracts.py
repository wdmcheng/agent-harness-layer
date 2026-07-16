"""可观测性本地优先写入与 provider fanout 合同测试。"""

from __future__ import annotations

from tests.contracts.test_observability_provider_adapters_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_observability_provider_adapters_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
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
