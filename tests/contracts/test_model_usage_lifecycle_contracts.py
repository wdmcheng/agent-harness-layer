"""Usage started/final event 关联与幂等合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    UsageEvidenceContext,
    UsageEvidenceLifecycle,
    model_usage_evidence,
)


@pytest.mark.asyncio
async def test_usage_lifecycle_reuses_correlation_and_final_event_id(tmp_path: Path) -> None:
    """验证 usage started/final 共用关联字段，且 final 重放以固定事件标识去重。"""

    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")

    async def resolve_trace(**_: object) -> str:
        """为本地事件总线提供确定 trace，避免 fixture 依赖持久化 run 查询。"""

        return "trace-a"

    bus = EventBus(sink=sink, run_trace_resolver=resolve_trace)
    evidence = model_usage_evidence(
        provider="fake",
        model="fake-basic",
        token_usage={"input_tokens": 3, "output_tokens": 2},
        latency_ms=4,
        decision={"route": "default", "provider_called": True},
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id="run-a",
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        ),
    )
    lifecycle = UsageEvidenceLifecycle(
        event_bus=bus,
        evidence=evidence,
        usage_call_id="usage-call-a",
    )

    started = await lifecycle.publish_started()
    final = await lifecycle.publish_final()
    replay = await lifecycle.publish_final()
    events = await sink.read(run_id="run-a")

    assert started.payload is not None
    assert final.payload is not None
    assert started.payload["correlation"] == final.payload["correlation"]
    assert final.event_id == replay.event_id
    assert final.terminal is False
    assert final.seq > started.seq
    assert final.payload["usage"]["trace_id"] == "trace-a"
    assert len(events) == 2


def test_usage_lifecycle_rejects_empty_call_id(tmp_path: Path) -> None:
    """公开 lifecycle seam 不得以随机 ID 掩盖缺失的 durable 调用关联。"""

    evidence = model_usage_evidence(
        provider="fake",
        model="fake-basic",
        token_usage={},
        latency_ms=0,
        decision={"provider_called": False},
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id="run-a",
            agent_id="agent-a",
            trace_id="trace-a",
        ),
    )
    with pytest.raises(ValueError, match="usage call id must not be empty"):
        UsageEvidenceLifecycle(
            event_bus=EventBus(sink=LocalJsonlEventSink(tmp_path / "events.jsonl")),
            evidence=evidence,
            usage_call_id="",
        )
