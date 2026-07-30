"""普通文本流 outbox 容量、状态机、sink 绑定与恢复合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import delete
from tests.contracts.model_usage_capacity_test_helpers import seed_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    UsageEvidenceContext,
    UsageEvidenceLifecycle,
    model_usage_evidence,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)
from agent_harness.storage.models import RunEvidenceOutboxModel
from agent_harness.storage.stream_evidence_repositories import (
    require_complete_settled_predecessors,
    stream_completed_event_id,
    stream_delta_event_id,
    stream_group_id,
    stream_usage_event_id,
)


def _stream_started_evidence(*, run_id: str):  # type: ignore[no-untyped-def]
    """构造携带 stream-usage-v1 marker 的最小 durable started identity。"""

    return model_usage_evidence(
        provider="fake",
        model="fake-local",
        token_usage={},
        latency_ms=0,
        decision={
            "provider_called": False,
            "usage_event_identity": {"ref": "stream-usage", "version": "v1"},
        },
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            trace_id="trace-a",
        ),
    )


@pytest.mark.asyncio
async def test_sqlite_claims_usage_and_65_stream_placeholders_in_one_uow(
    tmp_path: Path,
) -> None:
    """同一事务提交 2+65 槽位与稳定占位，幂等重放不得重复预约。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-capacity.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_call_id = "b" * 64
    try:
        run_id = await seed_run(storage)
        started = _stream_started_evidence(run_id=run_id)
        async with storage.uow() as uow:
            usage = await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
                event_id=stream_usage_event_id(usage_call_id, "final"),
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=started.to_payload(),
            )
            stream = await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
            )
            snapshot = await uow.event_capacity.snapshot(run_id)
            await uow.commit()

        assert usage.created is True
        assert stream.created is True
        assert len(stream.items) == 65
        assert [item.sequence_in_group for item in stream.items] == list(range(1, 66))
        assert all(
            item.state == "started" and item.reserved_event_count == 1 for item in stream.items
        )
        assert snapshot.outstanding_reserved_event_count == 67

        async with storage.uow() as uow:
            replay = await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
            )
            replay_snapshot = await uow.event_capacity.snapshot(run_id)
        assert replay.created is False
        assert replay_snapshot == snapshot
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_stream_claim_rolls_back_placeholders_and_capacity_with_outer_uow(
    tmp_path: Path,
) -> None:
    """后续同事务失败时，65 个占位与 outstanding 不能留下半提交。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-rollback.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_call_id = "c" * 64
    try:
        run_id = await seed_run(storage)
        with pytest.raises(RuntimeError, match="force rollback"):
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_stream(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                )
                raise RuntimeError("force rollback")

        async with storage.uow() as uow:
            assert (
                await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
                == []
            )
            snapshot = await uow.event_capacity.snapshot(run_id)
        assert snapshot.outstanding_reserved_event_count == 0
    finally:
        await storage.dispose()


def _delta_event(*, run_id: str, usage_call_id: str, ordinal: int, text: str) -> CanonicalEvent:
    """构造待固化的公开 delta intent；真实 seq 由 sink 在提交锁内分配。"""

    return CanonicalEvent(
        event_id=stream_delta_event_id(usage_call_id, ordinal),
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
        seq=0,
        payload={
            "correlation": {"usage_call_id": usage_call_id},
            "attempt": 1,
            "chunk_ordinal": ordinal,
            "text": text,
        },
        visibility="public",
        trace_id="trace-a",
    )


def _completed_event(
    *, run_id: str, usage_call_id: str, chunk_count: int, text: str
) -> CanonicalEvent:
    """构造与已发布安全文本一致的 completed intent。"""

    import hashlib

    encoded = text.encode("utf-8")
    return CanonicalEvent(
        event_id=stream_completed_event_id(usage_call_id),
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        event_type=CanonicalEventType.MODEL_OUTPUT_COMPLETED,
        seq=0,
        payload={
            "correlation": {"usage_call_id": usage_call_id},
            "attempt": 1,
            "chunk_count": chunk_count,
            "text_utf8_bytes": len(encoded),
            "text_sha256": hashlib.sha256(encoded).hexdigest(),
        },
        visibility="public",
        trace_id="trace-a",
    )


@pytest.mark.asyncio
async def test_stream_event_state_machine_is_ordered_and_replay_is_exact(tmp_path: Path) -> None:
    """占位必须先固化完整 intent，前驱未发布时后继不可发布，冲突重放关闭失败。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-state.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_call_id = "d" * 64
    try:
        run_id = await seed_run(storage)
        first = _delta_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            ordinal=1,
            text="甲",
        )
        second = _delta_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            ordinal=2,
            text="乙",
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a", run_id=run_id, usage_call_id=usage_call_id
            )
            persisted_second = await uow.evidence_outbox.persist_stream_event(second)
            assert persisted_second.state == "result_persisted"
            with pytest.raises(LookupError, match="predecessor"):
                await uow.evidence_outbox.ensure_event_publishable(event_id=second.event_id)
            persisted_first = await uow.evidence_outbox.persist_stream_event(first)
            replay = await uow.evidence_outbox.persist_stream_event(first)
            assert replay.result_json == persisted_first.result_json
            await uow.evidence_outbox.ensure_event_publishable(event_id=first.event_id)
            with pytest.raises(RuntimeError, match="conflict"):
                await uow.evidence_outbox.persist_stream_event(
                    first.model_copy(
                        update={
                            "payload": {
                                **cast(dict[str, Any], first.payload),
                                "text": "篡改",
                            }
                        }
                    )
                )
            await uow.commit()
    finally:
        await storage.dispose()


@pytest.mark.parametrize(
    "predecessors",
    [
        [(1, "published"), (1, "cancelled")],
        [(1, "published"), (3, "published")],
        [(2, "published")],
    ],
)
def test_stream_predecessor_validator_rejects_duplicate_or_non_contiguous_sequences(
    predecessors: list[tuple[int | None, str]],
) -> None:
    """有序发布必须看到从 1 开始的完整唯一前缀，不能只检查碰巧存在的行。"""

    with pytest.raises(LookupError, match="predecessor"):
        require_complete_settled_predecessors(
            current_sequence=3,
            predecessors=predecessors,
        )


@pytest.mark.asyncio
async def test_sqlite_stream_publish_rejects_missing_predecessor_row(tmp_path: Path) -> None:
    """SQLite 占位被破坏后，ordinal 2 即使自身已固化也不能越过缺失的 ordinal 1。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-missing-predecessor.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_call_id = "1" * 64
    try:
        run_id = await seed_run(storage)
        second = _delta_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            ordinal=2,
            text="不能越序",
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
            )
            await uow.evidence_outbox.persist_stream_event(second)
            await uow.session.execute(
                delete(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.event_id == stream_delta_event_id(usage_call_id, 1)
                )
            )
            with pytest.raises(LookupError, match="predecessor"):
                await uow.evidence_outbox.ensure_event_publishable(event_id=second.event_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_stream_cancels_only_unused_placeholders_and_releases_outstanding(
    tmp_path: Path,
) -> None:
    """成功收尾只释放 63 个未用 delta；已消费 delta 与 completed 各保留自己的槽。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-cancel.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_call_id = "e" * 64
    try:
        run_id = await seed_run(storage)
        first = _delta_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            ordinal=1,
            text="安全文本",
        )
        completed = _completed_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            chunk_count=1,
            text="安全文本",
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a", run_id=run_id, usage_call_id=usage_call_id
            )
            await uow.evidence_outbox.persist_stream_event(first)
            await uow.event_capacity.record_local_event(
                run_id=run_id,
                seq=1,
                reserved_event_count=1,
            )
            await uow.evidence_outbox.mark_event_published(event_id=first.event_id)
            cancelled = await uow.evidence_outbox.cancel_unused_stream(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
                used_delta_count=1,
                keep_completed=True,
            )
            assert cancelled == 63
            await uow.evidence_outbox.persist_stream_event(completed)
            await uow.evidence_outbox.ensure_event_publishable(event_id=completed.event_id)
            with pytest.raises(LookupError, match="completed stream evidence"):
                await uow.evidence_outbox.ensure_stream_settled_before_usage_final(
                    usage_call_id=usage_call_id,
                    outcome="completed",
                )
            await uow.event_capacity.record_local_event(
                run_id=run_id,
                seq=2,
                reserved_event_count=1,
            )
            await uow.evidence_outbox.mark_event_published(event_id=completed.event_id)
            await uow.evidence_outbox.ensure_stream_settled_before_usage_final(
                usage_call_id=usage_call_id,
                outcome="completed",
            )
            snapshot = await uow.event_capacity.snapshot(run_id)
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            await uow.commit()

        assert snapshot.highest_persisted_seq == 2
        assert snapshot.outstanding_reserved_event_count == 0
        assert group[0].state == "published"
        assert all(item.state == "cancelled" for item in group[1:64])
        assert group[64].state == "published"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_stream_usage_marker_selects_bounded_ids_and_legacy_stays_unchanged(
    tmp_path: Path,
) -> None:
    """只有 durable 精确 marker 选新 identity；无 marker 的历史调用不得被重键。"""

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    bus = EventBus(
        sink=LocalJsonlEventSink(tmp_path / "stream-usage.jsonl"),
        run_trace_resolver=resolve_trace,
    )
    usage_call_id = "f" * 64
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        trace_id="trace-a",
    )
    stream = UsageEvidenceLifecycle(
        event_bus=bus,
        evidence=model_usage_evidence(
            provider="fake",
            model="fake-local",
            token_usage={},
            latency_ms=0,
            decision={
                "provider_called": False,
                "usage_event_identity": {"ref": "stream-usage", "version": "v1"},
            },
            context=context,
        ),
        usage_call_id=usage_call_id,
    )
    started = await stream.publish_started()
    final = await stream.publish_final(outcome="cancelled")
    legacy = UsageEvidenceLifecycle(
        event_bus=bus,
        evidence=model_usage_evidence(
            provider="fake",
            model="fake-local",
            token_usage={},
            latency_ms=0,
            decision={"provider_called": False},
            context=context,
        ),
        usage_call_id="legacy-call",
    )

    assert started.event_id == stream_usage_event_id(usage_call_id, "started")
    assert final.event_id == stream_usage_event_id(usage_call_id, "final")
    assert legacy._event_id("started") == "usage:tenant-a:legacy-call:started"


@pytest.mark.asyncio
async def test_local_sink_binds_stream_payload_and_predecessor_before_capacity_use(
    tmp_path: Path,
) -> None:
    """local sink 不能凭 event id 消费 stream 槽，必须核对 durable payload 与前驱。"""

    from tests.contracts.model_usage_capacity_test_helpers import event_bus

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-local-sink.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_call_id = "1" * 64
    event_path = tmp_path / "stream-local-sink.jsonl"
    try:
        run_id = await seed_run(storage)
        first = _delta_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            ordinal=1,
            text="第一段",
        )
        second = _delta_event(
            run_id=run_id,
            usage_call_id=usage_call_id,
            ordinal=2,
            text="第二段",
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_stream(
                tenant_id="tenant-a", run_id=run_id, usage_call_id=usage_call_id
            )
            await uow.evidence_outbox.persist_stream_event(first)
            await uow.evidence_outbox.persist_stream_event(second)
            before = await uow.event_capacity.snapshot(run_id)
            await uow.commit()

        bus = event_bus(storage=storage, event_path=event_path)
        with pytest.raises(ValueError, match="durable stream intent"):
            await bus.publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
                payload={**cast(dict[str, Any], first.payload), "text": "篡改"},
                visibility="public",
                trace_id="trace-a",
                event_id=first.event_id,
            )
        with pytest.raises(LookupError, match="predecessor"):
            await bus.publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
                payload=cast(dict[str, Any], second.payload),
                visibility="public",
                trace_id="trace-a",
                event_id=second.event_id,
            )

        persisted = await bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
            payload=cast(dict[str, Any], first.payload),
            visibility="public",
            trace_id="trace-a",
            event_id=first.event_id,
        )
        async with storage.uow() as uow:
            after = await uow.event_capacity.snapshot(run_id)
        assert persisted.seq == 1
        assert after.highest_persisted_seq == 1
        assert after.outstanding_reserved_event_count == 64
        assert before.outstanding_reserved_event_count == 65
    finally:
        await storage.dispose()


@pytest.mark.parametrize(
    "marker",
    [
        None,
        {},
        {"ref": "stream-usage"},
        {"ref": "stream-usage", "version": "v2"},
        {"ref": "stream-usage", "version": "v1", "extra": True},
    ],
)
def test_present_but_malformed_stream_usage_marker_fails_closed(
    tmp_path: Path, marker: object
) -> None:
    """marker key 一旦存在就不能回退 legacy，避免损坏行换身份继续发布。"""

    evidence = model_usage_evidence(
        provider="fake",
        model="fake-local",
        token_usage={},
        latency_ms=0,
        decision={"provider_called": False, "usage_event_identity": marker},
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id="run-a",
            agent_id="agent-a",
            trace_id="trace-a",
        ),
    )
    with pytest.raises(ValueError, match="stream usage identity marker"):
        UsageEvidenceLifecycle(
            event_bus=EventBus(sink=LocalJsonlEventSink(tmp_path / "invalid.jsonl")),
            evidence=evidence,
            usage_call_id="a" * 64,
        )
