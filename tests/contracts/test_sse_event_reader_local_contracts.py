"""Local EventSink 有界 reader 公开合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventEnvelopeTooLarge,
    CanonicalEventType,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
    canonical_event_bytes,
    canonical_json_bytes,
)
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EventSequenceStateInvalid


async def _trace(**_: object) -> str:
    return "trace-a"


def _event(
    seq: int,
    *,
    visibility: str = "public",
    terminal: bool = False,
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=f"event-{seq}-{visibility}-{'terminal' if terminal else 'ordinary'}",
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        event_type=(
            CanonicalEventType.RUN_COMPLETED if terminal else CanonicalEventType.RUN_STARTED
        ),
        seq=seq,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload,
        terminal=terminal,
        visibility=visibility,
        trace_id="trace-a",
    )


def _write_direct(path: Path, events: list[CanonicalEvent]) -> None:
    """构造 legacy/direct-write 行，验证 reader 而不经过正常写端修正 seq。"""

    path.write_bytes(b"".join(canonical_json_bytes(event.to_payload()) + b"\n" for event in events))


def _sized_event(seq: int, target_bytes: int) -> CanonicalEvent:
    base = _event(seq, payload={"text": ""})
    overhead = len(canonical_json_bytes(base.to_payload()))
    assert target_bytes >= overhead
    sized = base.model_copy(update={"payload": {"text": "x" * (target_bytes - overhead)}})
    assert len(canonical_event_bytes(sized)) == target_bytes
    return sized


async def _create_run(storage: SQLAlchemyStorage) -> str:
    """为 relational sink 合同创建带 capacity/trace 的真实 run。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.create(
            SessionCreate(tenant_id="tenant-a", user_id="user-a", agent_id="agent-a")
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.commit()
    return run.id


def _sized_run_event(run_id: str, target_bytes: int) -> CanonicalEvent:
    """按最终 run_id 构造调用方 seq=0 时的精确 envelope 边界。"""

    base = _event(0, payload={"text": ""}).model_copy(
        update={"event_id": "event-final-seq-growth", "run_id": run_id}
    )
    overhead = len(canonical_json_bytes(base.to_payload()))
    sized = base.model_copy(update={"payload": {"text": "x" * (target_bytes - overhead)}})
    assert len(canonical_event_bytes(sized)) == target_bytes
    return sized


@pytest.mark.asyncio
async def test_local_reader_keeps_old_read_and_adds_visibility_membership_terminal_seams(
    tmp_path: Path,
) -> None:
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl", run_trace_resolver=_trace)
    public = await sink.write(_event(0))
    internal = await sink.write(_event(0, visibility="internal"))
    terminal = await sink.write(_event(0, terminal=True))

    assert [event.seq for event in await sink.read(run_id="run-a")] == [1, 2, 3]
    assert [event.seq for event in await sink.read_page(run_id="run-a")] == [1, 3]
    assert [
        event.seq
        for event in await sink.read_page(
            run_id="run-a",
            after_seq=public.seq,
            include_internal=True,
        )
    ] == [2, 3]
    assert await sink.contains_seq(run_id="run-a", seq=internal.seq) is False
    assert (
        await sink.contains_seq(
            run_id="run-a",
            seq=internal.seq,
            include_internal=True,
        )
        is True
    )
    assert await sink.contains_seq(run_id="run-a", seq=0) is False
    assert await sink.terminal_event(run_id="run-a") == terminal


@pytest.mark.asyncio
async def test_local_reader_enforces_100_event_page_without_loss_or_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    _write_direct(path, [_event(seq) for seq in range(1, 102)])
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    first = await sink.read_page(run_id="run-a")
    second = await sink.read_page(run_id="run-a", after_seq=first[-1].seq)

    assert [event.seq for event in first] == list(range(1, 101))
    assert [event.seq for event in second] == [101]


@pytest.mark.asyncio
async def test_local_100_event_page_does_not_parse_or_hold_the_next_page(tmp_path: Path) -> None:
    path = tmp_path / "bounded-read.jsonl"
    path.write_bytes(
        b"".join(canonical_json_bytes(_event(seq).to_payload()) + b"\n" for seq in range(1, 101))
        + b"{this-is-the-next-page-and-is-not-json}\n"
    )
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    page = await sink.read_page(run_id="run-a")

    assert [event.seq for event in page] == list(range(1, 101))


@pytest.mark.asyncio
async def test_local_reader_rejects_non_increasing_direct_write_sequence(tmp_path: Path) -> None:
    path = tmp_path / "out-of-order.jsonl"
    _write_direct(path, [_event(2), _event(1)])
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    with pytest.raises(EventSequenceStateInvalid) as rejected:
        await sink.read_page(run_id="run-a")
    assert rejected.value.code == "event.sequence_state_invalid"


@pytest.mark.asyncio
async def test_local_reader_enforces_canonical_byte_page_on_complete_event_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    events = [_event(seq, payload={"text": "中" * 20_000}) for seq in range(1, 20)]
    assert all(len(canonical_event_bytes(event)) <= 65_536 for event in events)
    _write_direct(path, events)
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    expected_count = 0
    accumulated = 0
    for event in events:
        encoded_size = len(canonical_event_bytes(event))
        if accumulated + encoded_size > 1_048_576:
            break
        accumulated += encoded_size
        expected_count += 1
    first = await sink.read_page(run_id="run-a")
    second = await sink.read_page(run_id="run-a", after_seq=first[-1].seq)

    assert len(first) == expected_count
    assert sum(len(canonical_event_bytes(event)) for event in first) <= 1_048_576
    assert second[0].seq == first[-1].seq + 1
    assert [event.seq for event in first + second] == list(range(1, 20))


@pytest.mark.asyncio
async def test_local_reader_accepts_exact_1mib_and_defers_1048577th_byte_boundary(
    tmp_path: Path,
) -> None:
    exact_path = tmp_path / "exact-page.jsonl"
    exact_events = [_sized_event(seq, 65_536) for seq in range(1, 17)]
    next_page_oversized = _event(17, payload={"text": "中" * 30_000})
    assert len(canonical_json_bytes(next_page_oversized.to_payload())) > 65_536
    _write_direct(exact_path, [*exact_events, next_page_oversized])
    exact_page = await LocalJsonlEventSink(
        exact_path,
        run_trace_resolver=_trace,
    ).read_page(run_id="run-a")
    assert sum(len(canonical_event_bytes(event)) for event in exact_page) == 1_048_576
    assert [event.seq for event in exact_page] == list(range(1, 17))

    overflow_path = tmp_path / "overflow-page.jsonl"
    overflow_events = [
        *[_sized_event(seq, 65_536) for seq in range(1, 16)],
        _sized_event(16, 65_137),
        _sized_event(17, 400),
    ]
    assert sum(len(canonical_event_bytes(event)) for event in overflow_events) == 1_048_577
    _write_direct(overflow_path, overflow_events)
    sink = LocalJsonlEventSink(overflow_path, run_trace_resolver=_trace)

    first = await sink.read_page(run_id="run-a")
    second = await sink.read_page(run_id="run-a", after_seq=first[-1].seq)
    assert [event.seq for event in first] == list(range(1, 17))
    assert [event.seq for event in second] == [17]


@pytest.mark.asyncio
async def test_local_reader_fails_on_next_legacy_oversized_row_without_empty_page_or_skip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    valid = _event(1)
    oversized = _event(2, payload={"text": "中" * 30_000})
    trailing = _event(3)
    _write_direct(path, [valid, oversized, trailing])
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    first = await sink.read_page(run_id="run-a", max_events=1)
    assert first == [valid]
    with pytest.raises(CanonicalEventEnvelopeStateInvalid) as rejected:
        await sink.read_page(run_id="run-a", after_seq=1)
    assert rejected.value.code == "event.envelope_state_invalid"


@pytest.mark.asyncio
async def test_local_reader_validates_every_row_in_authorized_visibility_view(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hidden-oversized.jsonl"
    hidden = _event(1, visibility="internal", payload={"text": "中" * 30_000})
    public = _event(2)
    _write_direct(path, [hidden, public])
    sink = LocalJsonlEventSink(path, run_trace_resolver=_trace)

    assert await sink.read_page(run_id="run-a") == [public]
    with pytest.raises(CanonicalEventEnvelopeStateInvalid):
        await sink.read_page(run_id="run-a", include_internal=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_events", "max_bytes"),
    [(0, 1_048_576), (101, 1_048_576), (1, 0), (1, 1_048_577)],
)
async def test_local_reader_rejects_limits_outside_contract_hard_bounds(
    tmp_path: Path,
    max_events: int,
    max_bytes: int,
) -> None:
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl", run_trace_resolver=_trace)

    with pytest.raises(ValueError):
        await sink.read_page(
            run_id="run-a",
            max_events=max_events,
            max_bytes=max_bytes,
        )


@pytest.mark.asyncio
async def test_sqlite_relational_reader_uses_same_visibility_page_and_terminal_seams(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'reader.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        run_id = await _create_run(storage)
        sink = PostgreSQLEventSink(storage)
        await sink.write(_event(1).model_copy(update={"run_id": run_id}))
        internal = await sink.write(
            _event(2, visibility="internal").model_copy(update={"run_id": run_id})
        )
        terminal = await sink.write(_event(3, terminal=True).model_copy(update={"run_id": run_id}))

        assert [event.seq for event in await sink.read_page(run_id=run_id)] == [1, 3]
        assert await sink.contains_seq(run_id=run_id, seq=internal.seq) is False
        assert (
            await sink.contains_seq(
                run_id=run_id,
                seq=internal.seq,
                include_internal=True,
            )
            is True
        )
        assert await sink.terminal_event(run_id=run_id) == terminal
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_relational_sink_revalidates_size_after_assigning_final_seq(tmp_path: Path) -> None:
    """seq 位数增长不能把 65536B 输入持久化成 65537B envelope。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'final-size.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        run_id = await _create_run(storage)
        sink = PostgreSQLEventSink(storage)
        for seq in range(1, 10):
            await sink.write(
                _event(seq).model_copy(update={"event_id": f"seed-{seq}", "run_id": run_id})
            )
        before = await sink.read(run_id=run_id)
        boundary = _sized_run_event(run_id, 65_536)

        with pytest.raises(CanonicalEventEnvelopeTooLarge):
            await sink.write(boundary)

        assert await sink.read(run_id=run_id) == before
    finally:
        await storage.dispose()
