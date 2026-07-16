"""本地事件重放、幂等与原子 claim 合同测试。"""

from __future__ import annotations

from tests.contracts.test_canonical_events_artifacts_contracts import (
    Any as Any,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    CanonicalEvent as CanonicalEvent,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    ContractRunTraceResolver as ContractRunTraceResolver,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    Path as Path,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    _write_local_event_in_process as _write_local_event_in_process,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    multiprocessing as multiprocessing,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    pytest as pytest,
)


@pytest.mark.asyncio
async def test_local_sink_replay_is_global_idempotent_and_boundary_safe(tmp_path: Path) -> None:
    """direct local sink 的 event-id claim 跨 run/tenant，且错误不泄露已有事件。"""

    path = tmp_path / "events.jsonl"
    sink = LocalJsonlEventSink(
        path,
        run_trace_resolver=ContractRunTraceResolver({("tenant-a", "run-a"): "trace-a"}),
    )
    event = CanonicalEvent(
        event_id="shared-event-id",
        tenant_id="tenant-a",
        run_id="run-a",
        event_type=CanonicalEventType.RUN_STARTED,
        seq=0,
        trace_id="trace-a",
        record_scope="non_run",
    )
    persisted = await sink.write(event)
    assert await sink.write(event) == persisted
    assert (
        await asyncio.gather(*(LocalJsonlEventSink(path).write(event) for _ in range(6)))
        == [persisted] * 6
    )

    invalid_replays = [
        event.model_copy(update={"tenant_id": "tenant-b"}),
        event.model_copy(update={"run_id": "run-b"}),
        event.model_copy(update={"trace_id": "trace-b"}),
        event.model_copy(update={"record_scope": "run"}),
        event.model_copy(
            update={
                "event_type": CanonicalEventType.RUN_COMPLETED,
                "terminal": True,
                "visibility": "public",
            }
        ),
        event.model_copy(update={"visibility": "public"}),
        event.model_copy(update={"event_type": CanonicalEventType.TOOL_CALL_COMPLETED}),
        event.model_copy(update={"event_version": "2.0"}),
        event.model_copy(update={"user_id": "other-user"}),
        event.model_copy(update={"agent_id": "other-agent"}),
        event.model_copy(update={"parent_run_id": "other-parent"}),
        event.model_copy(update={"payload": {"operation": "different"}}),
        event.model_copy(update={"payload_ref": "artifact://different"}),
        event.model_copy(update={"payload_checksum": "different-checksum"}),
        event.model_copy(update={"raw_event_ref": "provider://different"}),
        event.model_copy(update={"request_id": "different-request"}),
        event.model_copy(update={"span_id": "different-span"}),
    ]
    for replay in invalid_replays:
        with pytest.raises(
            ValueError,
            match="event replay envelope does not match persisted event",
        ) as error:
            await sink.write(replay)
        assert "tenant-a" not in str(error.value)
        assert "run-a" not in str(error.value)
        assert "trace-a" not in str(error.value)

    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert await sink.read(run_id="run-b") == []


@pytest.mark.asyncio
async def test_event_bus_replay_matrix_delegates_to_atomic_local_claim(tmp_path: Path) -> None:
    """EventBus 不预查盲返，所有 replay 边界都由同一个 local claim 校验。"""

    path = tmp_path / "events.jsonl"
    bus = EventBus(
        sink=LocalJsonlEventSink(path),
        run_trace_resolver=ContractRunTraceResolver({("tenant-a", "run-a"): "trace-a"}),
    )

    async def publish(**updates: Any) -> CanonicalEvent:
        values: dict[str, Any] = {
            "tenant_id": "tenant-a",
            "run_id": "run-a",
            "trace_id": "trace-a",
            "record_scope": "non_run",
            "event_type": CanonicalEventType.RUN_STARTED,
            "terminal": False,
            "visibility": "internal",
        }
        values.update(updates)
        return await bus.publish(
            tenant_id=values["tenant_id"],
            run_id=values["run_id"],
            event_type=values["event_type"],
            event_id="bus-matrix-event",
            trace_id=values["trace_id"],
            record_scope=values["record_scope"],
            terminal=values["terminal"],
            visibility=values["visibility"],
        )

    persisted = await publish()
    assert await publish() == persisted
    invalid_updates = [
        {"tenant_id": "tenant-b"},
        {"run_id": "run-b"},
        {"trace_id": "trace-b"},
        {"record_scope": "run"},
        {
            "event_type": CanonicalEventType.RUN_COMPLETED,
            "terminal": True,
            "visibility": "public",
        },
        {"visibility": "public"},
    ]
    for updates in invalid_updates:
        with pytest.raises(
            ValueError,
            match="event replay envelope does not match persisted event",
        ) as error:
            await publish(**updates)
        assert str(error.value) == "event replay envelope does not match persisted event"

    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_local_sink_same_id_race_is_idempotent_across_processes(tmp_path: Path) -> None:
    """两个独立进程同时 append 同 ID，最终只能有一条 evidence。"""

    path = tmp_path / "events.jsonl"
    event = CanonicalEvent(
        event_id="process-race-event",
        tenant_id="tenant-process",
        run_id="run-process",
        event_type=CanonicalEventType.RUN_STARTED,
        seq=0,
        trace_id="trace-process",
        record_scope="non_run",
    )
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_write_local_event_in_process,
            args=(str(path), event.to_payload(), start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert outcomes == [("ok", 1), ("ok", 1)]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
