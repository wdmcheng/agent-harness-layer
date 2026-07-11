"""CanonicalEvent、artifact 和 local evidence 的公开契约测试。

这些测试锁的是 runtime/API/eval 后续都会消费的事件脊柱：事件 envelope、
per-run seq、terminal 唯一性、payload_ref、redaction、OTel 映射和 SSE 格式。
它们不证明真实观测 provider、模型调用或多进程 worker；那些属于后续能力。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts import GuardrailDecision, SourceRef, TrustLevel
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    TerminalEventError,
)
from agent_harness.observability.otel import map_event_to_otel
from agent_harness.security.guardrails import guardrail_event_payload
from app.api.sse import format_sse_event

ROOT = Path(__file__).resolve().parents[2]


def test_event_bus_coordinates_each_event_loop_without_cross_loop_lock_binding() -> None:
    """同一 service EventBus 经 DBOS loop 和 worker loop 连续竞争时都可发布。"""

    class YieldingSink:
        def __init__(self) -> None:
            self.events: list[CanonicalEvent] = []

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            await asyncio.sleep(0)
            self.events.append(event)
            return event

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            return [
                event for event in self.events if event.run_id == run_id and event.seq > after_seq
            ]

        async def latest_seq(self, run_id: str) -> int:
            return max(
                (event.seq for event in self.events if event.run_id == run_id),
                default=0,
            )

        async def has_terminal(self, run_id: str) -> bool:
            return any(event.run_id == run_id and event.terminal for event in self.events)

    sink = YieldingSink()
    bus = EventBus(sink=sink)

    async def burst(prefix: str) -> None:
        await asyncio.gather(
            *(
                bus.publish(
                    tenant_id="tenant-loop",
                    run_id="run-loop",
                    event_type=CanonicalEventType.RUN_STARTED,
                    event_id=f"{prefix}-{index}",
                )
                for index in range(2)
            )
        )

    asyncio.run(burst("dbos"))
    asyncio.run(burst("worker"))

    assert [event.seq for event in sink.events] == [1, 2, 3, 4]


def test_canonical_event_type_catalog_contains_product_spec_p0_events() -> None:
    # Product-Spec 的 P0 catalog 是 adapter 互通的枚举契约。这里锁完整清单，
    # 防止只测 happy path 的 run/guardrail 事件时漏掉 eval 或 compaction 事件。
    expected = {
        "run.queued",
        "run.started",
        "run.resumed",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "model.request.started",
        "model.output.delta",
        "model.output.completed",
        "model.structured.delta",
        "model.structured.completed",
        "model.usage.updated",
        "input.guardrail.checked",
        "input.guardrail.blocked",
        "reasoning.delta",
        "tool.call.args_delta",
        "tool.call.started",
        "tool.call.completed",
        "tool.call.failed",
        "retrieval.query.started",
        "retrieval.query.completed",
        "context.assembly.started",
        "context.assembly.completed",
        "policy.decision",
        "approval.required",
        "approval.resolved",
        "checkpoint.created",
        "context.compaction.started",
        "context.compaction.completed",
        "eval.case.drafted",
        "eval.case.approved",
        "eval.run.started",
        "eval.run.completed",
        "eval.score.recorded",
    }

    assert expected <= {event_type.value for event_type in CanonicalEventType}


@pytest.mark.asyncio
async def test_event_bus_assigns_seq_and_rejects_second_terminal(tmp_path: Path) -> None:
    # EventBus 是 runtime 的写入 seam：调用方不手动分配 seq，也不能绕过 terminal 规则。
    # 这里用 local jsonl sink 证明断线续读语义；不声明它已经支持多进程并发队列。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    bus = EventBus(sink=sink)

    first = await bus.publish(
        tenant_id="default",
        run_id="run-1",
        user_id="user-1",
        agent_id="fake-agent",
        parent_run_id="parent-run",
        event_type=CanonicalEventType.RUN_STARTED,
        payload={"step": "start"},
        raw_event_ref="provider://raw/run-1/1",
        trace_id="trace-1",
        span_id="span-1",
    )
    terminal = await bus.publish(
        tenant_id="default",
        run_id="run-1",
        agent_id="fake-agent",
        event_type=CanonicalEventType.RUN_COMPLETED,
        payload={"status": "completed"},
        terminal=True,
    )

    # Envelope 字段是外部 adapter 之间的稳定协议；这里锁字段名，避免实现
    # 悄悄退回 provider-specific id 或缺少 trace/resume 所需的关联字段。
    assert first.event_id
    assert first.event_version == "1.0"
    assert first.user_id == "user-1"
    assert first.parent_run_id == "parent-run"
    assert first.raw_event_ref == "provider://raw/run-1/1"
    assert first.span_id == "span-1"
    assert first.seq == 1
    assert terminal.seq == 2
    assert terminal.terminal is True
    assert [event.seq for event in await sink.read(run_id="run-1", after_seq=1)] == [2]

    with pytest.raises(TerminalEventError):
        await bus.publish(
            tenant_id="default",
            run_id="run-1",
            agent_id="fake-agent",
            event_type=CanonicalEventType.RUN_FAILED,
            payload={"status": "failed"},
            terminal=True,
        )


@pytest.mark.asyncio
async def test_large_payload_is_written_to_artifact_ref(tmp_path: Path) -> None:
    # 大 payload 不能塞进事件正文；事件只保存摘要、payload_ref 和 checksum。
    # artifact store 保留脱敏后的完整证据，后续 eval/debug 可以按 ref 取回。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    store = FileArtifactStore(tmp_path / "artifacts")
    bus = EventBus(sink=sink, artifact_store=store, inline_payload_bytes=32)
    payload = {"text": "x" * 200}

    event = await bus.publish(
        tenant_id="default",
        run_id="run-large",
        agent_id="fake-agent",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        payload=payload,
    )

    assert event.payload_ref is not None
    assert event.payload_checksum is not None
    assert event.payload == {"artifact": {"size_bytes": len(json.dumps(payload).encode())}}
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
    bus = EventBus(sink=sink)

    event = await bus.publish(
        tenant_id="default",
        run_id="run-guardrail",
        agent_id="fake-agent",
        event_type=CanonicalEventType.INPUT_GUARDRAIL_BLOCKED,
        payload=payload,
        terminal=True,
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
    bus = EventBus(sink=sink, artifact_store=store, inline_payload_bytes=32)
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
    event = await EventBus(sink=sink).publish(
        tenant_id="default",
        run_id="run-sse",
        agent_id="fake-agent",
        event_type=CanonicalEventType.RUN_COMPLETED,
        payload={"status": "completed"},
        terminal=True,
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
