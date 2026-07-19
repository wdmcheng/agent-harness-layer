"""不引入 CanonicalEvent retention 的范围与持久性合同。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_harness.events import CanonicalEvent, CanonicalEventType, LocalJsonlEventSink


async def _trace(**_: object) -> str:
    return "trace-a"


def _event(seq: int, *, terminal: bool = False) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=f"retention-contract-{seq}-{'terminal' if terminal else 'ordinary'}",
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        event_type=(
            CanonicalEventType.RUN_COMPLETED if terminal else CanonicalEventType.RUN_STARTED
        ),
        seq=seq,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        terminal=terminal,
        visibility="public",
        trace_id="trace-a",
    )


@pytest.mark.asyncio
async def test_past_visible_cursor_survives_local_reader_recreation(tmp_path: Path) -> None:
    """reader 重建不得让 run 存续期内已可见 cursor 变成 expired。"""

    event_path = tmp_path / "durable-events.jsonl"
    writer = LocalJsonlEventSink(event_path, run_trace_resolver=_trace)
    first = await writer.write(_event(0))
    terminal = await writer.write(_event(0, terminal=True))

    reopened = LocalJsonlEventSink(event_path, run_trace_resolver=_trace)
    assert await reopened.contains_seq(run_id="run-a", seq=first.seq) is True
    assert await reopened.read_page(run_id="run-a", after_seq=first.seq) == [terminal]
    assert await reopened.contains_seq(run_id="run-a", seq=terminal.seq) is True


def test_p0_has_no_canonical_event_cleanup_ttl_or_retention_surface() -> None:
    """若未来加入 event 过期行为，本合同迫使其先进入独立 change。"""

    root = Path(__file__).parents[2]
    event_sources = list((root / "packages/agent-harness/src/agent_harness/events").rglob("*.py"))
    worker_sources = list((root / "templates/service-app/app/workers").rglob("*.py"))
    deployment_sources = [root / "templates/service-app/docker-compose.yml"]
    sources = [*event_sources, *worker_sources, *deployment_sources]
    forbidden = re.compile(
        r"\bcanonical_events?\b.{0,80}\b(delete|cleanup|retention|ttl|expire[ds]?)\b"
        r"|\b(delete|cleanup|retention|ttl|expire[ds]?)\b.{0,80}\bcanonical_events?\b",
        re.IGNORECASE | re.DOTALL,
    )

    matches = [path.relative_to(root) for path in sources if forbidden.search(path.read_text())]
    assert matches == []
