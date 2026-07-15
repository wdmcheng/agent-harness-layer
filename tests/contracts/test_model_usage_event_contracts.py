"""Model usage event、canonical serializer 与 terminal 顺序合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventEnvelopeTooLarge,
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    TerminalEventError,
    canonical_event_bytes,
    canonical_json_bytes,
)

_MISSING = object()


def valid_usage_payload(**updates: object) -> dict[str, object]:
    """构造 EventBus final usage 校验使用的完整 provider-neutral DTO。"""

    payload: dict[str, object] = {
        "usage_kind": "model",
        "tenant_id": "tenant-a",
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 3,
        "decision": {"provider_called": True},
        "run_id": "run-a",
        "agent_id": "agent-a",
        "request_id": None,
        "trace_id": "trace-a",
    }
    payload.update(updates)
    return payload


def event(
    *, event_type: CanonicalEventType, seq: int = 1, payload: dict[str, object] | None = None
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=f"event-{seq}",
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        event_type=event_type,
        seq=seq,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload,
        terminal=event_type
        in {
            CanonicalEventType.RUN_COMPLETED,
            CanonicalEventType.RUN_FAILED,
            CanonicalEventType.RUN_CANCELLED,
        },
        visibility="public" if event_type.value.startswith("run.") else "internal",
        trace_id="trace-a",
    )


def test_canonical_event_bytes_is_order_and_unicode_stable() -> None:
    first = event(
        event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
        payload={"z": "中文", "a": {"b": 1, "a": 2}},
    )
    second = first.model_copy(update={"payload": {"a": {"a": 2, "b": 1}, "z": "中文"}})

    assert canonical_event_bytes(first) == canonical_event_bytes(second)
    assert b"\\u4e2d" not in canonical_event_bytes(first)


def test_canonical_event_bytes_has_stable_json_escaping() -> None:
    serialized = canonical_event_bytes(
        event(
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            payload={"text": 'quote=" slash=\\ newline=\n 中文'},
        )
    ).decode("utf-8")

    assert '\\"' in serialized
    assert "\\\\" in serialized
    assert "\\n" in serialized
    assert "中文" in serialized


def test_canonical_event_bytes_rejects_nan() -> None:
    with pytest.raises(ValueError, match="finite"):
        canonical_event_bytes(
            event(
                event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
                payload={"value": float("nan")},
            )
        )


def test_canonical_event_bytes_rejects_envelope_over_hard_limit() -> None:
    invalid = event(
        event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
        payload={"value": "x" * 70_000},
    )

    with pytest.raises(CanonicalEventEnvelopeTooLarge):
        canonical_event_bytes(invalid)


def test_canonical_event_bytes_accepts_exact_limit_and_rejects_next_byte() -> None:
    base = event(
        event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
        payload={"value": ""},
    )
    overhead = len(canonical_json_bytes(base.to_payload()))
    exact = base.model_copy(update={"payload": {"value": "x" * (65_536 - overhead)}})
    over = base.model_copy(update={"payload": {"value": "x" * (65_537 - overhead)}})

    assert len(canonical_event_bytes(exact)) == 65_536
    with pytest.raises(CanonicalEventEnvelopeTooLarge) as exc_info:
        canonical_event_bytes(over)
    assert exc_info.value.code == "event.envelope_too_large"


@pytest.mark.asyncio
async def test_artifact_externalization_still_rejects_oversized_envelope_before_write(
    tmp_path: Path,
) -> None:
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    bus = EventBus(
        sink=sink,
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        run_trace_resolver=_trace,
    )

    with pytest.raises(CanonicalEventEnvelopeTooLarge):
        await bus.publish(
            tenant_id="tenant-a",
            run_id="run-a",
            agent_id="a" * 70_000,
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            trace_id="trace-a",
            payload={
                "correlation": {"usage_call_id": "usage-oversized"},
                "usage": valid_usage_payload(agent_id="a" * 70_000),
                "raw": "payload" * 10_000,
            },
        )
    assert not (tmp_path / "events.jsonl").exists()
    assert list((tmp_path / "artifacts").glob("**/*")) == []


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("input_tokens", -1, id="negative-token"),
        pytest.param("output_tokens", True, id="bool-token"),
        pytest.param("cost_usd", float("nan"), id="nan-cost"),
        pytest.param("cost_usd", float("inf"), id="infinite-cost"),
        pytest.param("trace_id", _MISSING, id="missing-required-field"),
    ],
)
@pytest.mark.asyncio
async def test_event_bus_rejects_invalid_final_usage_before_persistence(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    """最终 usage 必须先通过统一 DTO；非法值不能留下 event 或 artifact。"""

    events_path = tmp_path / "invalid-usage.jsonl"
    artifact_root = tmp_path / "artifacts"
    usage = valid_usage_payload()
    if invalid_value is _MISSING:
        usage.pop(field)
    else:
        usage[field] = invalid_value
    bus = EventBus(
        sink=LocalJsonlEventSink(events_path),
        artifact_store=FileArtifactStore(artifact_root),
        run_trace_resolver=_trace,
    )

    with pytest.raises(ValueError):
        await bus.publish(
            tenant_id="tenant-a",
            run_id="run-a",
            agent_id="agent-a",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            trace_id="trace-a",
            payload={
                "correlation": {"usage_call_id": "invalid-usage"},
                "usage": usage,
                "outcome": "completed",
            },
        )

    assert not events_path.exists()
    assert not artifact_root.exists()


@pytest.mark.asyncio
async def test_local_reader_fails_closed_on_legacy_oversized_envelope(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jsonl"
    oversized = event(
        event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
        payload={"value": "x" * 70_000},
    )
    path.write_text(
        canonical_json_bytes(oversized.to_payload()).decode("utf-8") + "\n",
        encoding="utf-8",
    )
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    with pytest.raises(CanonicalEventEnvelopeStateInvalid) as exc_info:
        await sink.read(run_id="run-a")
    assert exc_info.value.code == "event.envelope_state_invalid"


@pytest.mark.asyncio
async def test_event_bus_rejects_any_event_after_public_terminal(tmp_path: Path) -> None:
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    bus = EventBus(sink=sink, run_trace_resolver=_trace)

    await bus.publish(
        tenant_id="tenant-a",
        run_id="run-a",
        event_type=CanonicalEventType.RUN_COMPLETED,
        trace_id="trace-a",
        terminal=True,
        visibility="public",
    )

    with pytest.raises(TerminalEventError, match="run already has terminal event"):
        await bus.publish(
            tenant_id="tenant-a",
            run_id="run-a",
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            trace_id="trace-a",
            payload={"late": True},
        )


async def _trace(**_: object) -> str:
    return "trace-a"
