"""Event payload artifact、secret 脱敏与 provider-neutral 映射契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.contracts.canonical_event_artifact_test_helpers import ContractRunTraceResolver

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts import GuardrailDecision, SourceRef, TrustLevel
from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    canonical_event_bytes,
)
from agent_harness.events.serialization import canonical_json_bytes
from agent_harness.observability.otel import map_event_to_otel
from agent_harness.security.guardrails import guardrail_event_payload
from app.api.sse import format_sse_event


@pytest.mark.asyncio
async def test_large_payload_is_written_to_artifact_ref(tmp_path: Path) -> None:
    # 大 payload 不能塞进事件正文；事件只保存摘要、payload_ref 和 checksum。
    # artifact store 保留脱敏后的完整证据，后续 eval/debug 可以按 ref 取回。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    store = FileArtifactStore(tmp_path / "artifacts")
    bus = EventBus(
        sink=sink,
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("default", "run-large"): "trace-large"}),
    )
    payload = {"text": "x" * 200}

    event = await bus.publish(
        tenant_id="default",
        run_id="run-large",
        agent_id="fake-agent",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        payload=payload,
        trace_id="trace-large",
    )

    assert event.payload_ref is not None
    assert event.payload_checksum is not None
    assert event.payload == {"artifact": {"size_bytes": len(canonical_json_bytes(payload))}}
    assert store.read_json(event.payload_ref) == payload


@pytest.mark.asyncio
async def test_guardrail_payload_redacts_secret_metadata(tmp_path: Path) -> None:
    # guardrail/context assembly 事件只允许摘要和来源元数据，secret-like 字段必须脱敏。
    # 这里直接通过 helper + EventBus 持久化，证明写入证据前就已经 redaction。
    source = SourceRef(kind="user", uri="prompt://local", label="local prompt")
    payload = guardrail_event_payload(
        decision=GuardrailDecision.deny(
            "blocked",
            metadata={
                "api_key": "sk-secret",
                "nested": {"password": "p@ss"},
                "raw_tool_output": "token=tool-secret-12345 and sk-abcdef1234567890",
            },
        ),
        source_ref=source,
        trust_level=TrustLevel.UNTRUSTED,
        summary="user prompt blocked token=summary-secret-12345",
        truncated=True,
    )
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    bus = EventBus(
        sink=sink,
        run_trace_resolver=ContractRunTraceResolver(
            {("default", "run-guardrail"): "trace-guardrail"}
        ),
    )

    event = await bus.publish(
        tenant_id="default",
        run_id="run-guardrail",
        agent_id="fake-agent",
        event_type=CanonicalEventType.INPUT_GUARDRAIL_BLOCKED,
        payload=payload,
        terminal=False,
        visibility="public",
        trace_id="trace-guardrail",
    )

    persisted = event.to_payload()
    assert "sk-secret" not in json.dumps(persisted)
    assert "p@ss" not in json.dumps(persisted)
    assert "tool-secret-12345" not in json.dumps(persisted)
    assert "sk-abcdef1234567890" not in json.dumps(persisted)
    assert "summary-secret-12345" not in json.dumps(persisted)
    assert "[REDACTED]" in persisted["payload"]["summary"]
    assert persisted["payload"]["decision"]["metadata"]["api_key"] == "[REDACTED]"
    assert persisted["payload"]["trust_level"] == "untrusted"


@pytest.mark.asyncio
async def test_artifact_payload_is_redacted_before_disk_write(tmp_path: Path) -> None:
    # 大 payload 会进入 artifact，而 artifact 是长期证据文件。这里直接读回磁盘内容，
    # 证明 secret 不只是从事件摘要中消失，也没有落到 artifact 本体。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    store = FileArtifactStore(tmp_path / "artifacts")
    bus = EventBus(
        sink=sink,
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver(
            {("default", "run-artifact-secret"): "trace-artifact-secret"}
        ),
    )
    payload = {
        "api_key": "sk-abcdef1234567890",
        "text": "large-secret-payload token=artifact-secret-12345" * 8,
    }

    event = await bus.publish(
        tenant_id="default",
        run_id="run-artifact-secret",
        agent_id="fake-agent",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        payload=payload,
        trace_id="trace-artifact-secret",
    )

    assert event.payload_ref is not None
    artifact = store.read_json(event.payload_ref)
    serialized = json.dumps(artifact)
    assert "sk-abcdef1234567890" not in serialized
    assert "artifact-secret-12345" not in serialized
    assert artifact["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_otel_mapping_and_sse_format_are_provider_neutral(tmp_path: Path) -> None:
    # OTel facade 和 SSE adapter 都消费 CanonicalEvent JSON，不允许暴露 provider SDK、
    # ORM model 或内部 Python 对象。真实 exporter 和 FastAPI route 在后续能力中接入。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    event = await EventBus(
        sink=sink,
        run_trace_resolver=ContractRunTraceResolver({("default", "run-sse"): "trace-1"}),
    ).publish(
        tenant_id="default",
        run_id="run-sse",
        agent_id="fake-agent",
        event_type=CanonicalEventType.RUN_COMPLETED,
        payload={"status": "completed"},
        terminal=True,
        visibility="public",
        trace_id="trace-1",
    )

    otel = map_event_to_otel(event)
    sse = format_sse_event(event)

    assert otel.name == "agent_harness.run.completed"
    assert otel.attributes["event_id"] == event.event_id
    assert otel.attributes["run_id"] == "run-sse"
    assert otel.attributes["event_type"] == "run.completed"
    assert "id: 1" in sse
    assert "event: run.completed" in sse
    assert '"run_id":"run-sse"' in sse
    assert f"data: {canonical_event_bytes(event).decode('utf-8')}\n\n" in sse
