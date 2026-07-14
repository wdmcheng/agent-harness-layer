"""Local event claim 与 artifact 补偿原子性的公开契约测试。"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Any, cast

import pytest
from tests.contracts.canonical_event_artifact_test_helpers import (
    ContractRunTraceResolver,
    FailingArtifactStore,
    RecordingArtifactStore,
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink


@pytest.mark.asyncio
async def test_event_bus_claim_precedes_artifact_and_boundary_collision_fanout(
    tmp_path: Path,
) -> None:
    """EventBus collision 不 materialize 碰撞方 payload，也不能进入后续 fan-out。"""

    path = tmp_path / "events.jsonl"
    store = RecordingArtifactStore(tmp_path / "artifacts")
    resolver = ContractRunTraceResolver(
        {
            ("tenant-a", "run-a"): "trace-a",
            ("tenant-b", "run-b"): "trace-b",
        }
    )
    first_bus = EventBus(
        sink=LocalJsonlEventSink(path),
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=resolver,
    )
    second_bus = EventBus(
        sink=LocalJsonlEventSink(path),
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=resolver,
    )
    fanout: list[str] = []

    async def publish_and_fanout(bus: EventBus, **kwargs: Any) -> CanonicalEvent:
        published = await bus.publish(**kwargs)
        fanout.append(published.event_id)
        return published

    first = await publish_and_fanout(
        first_bus,
        tenant_id="tenant-a",
        run_id="run-a",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        event_id="artifact-claim",
        trace_id="trace-a",
        payload={"text": "a" * 256},
    )
    retry = await publish_and_fanout(
        second_bus,
        tenant_id="tenant-a",
        run_id="run-a",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        event_id="artifact-claim",
        trace_id="trace-a",
        payload={"text": "a" * 256},
    )
    assert retry == first
    assert len(store.payloads) == 1
    assert len(list(store.root.glob("*.json"))) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    with pytest.raises(ValueError, match="event replay envelope does not match persisted event"):
        await publish_and_fanout(
            second_bus,
            tenant_id="tenant-a",
            run_id="run-a",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="artifact-claim",
            trace_id="trace-a",
            payload={"text": "different" * 64},
        )
    assert fanout == ["artifact-claim", "artifact-claim"]
    assert len(store.payloads) == 1
    assert len(list(store.root.glob("*.json"))) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    with pytest.raises(ValueError, match="event replay envelope does not match persisted event"):
        await publish_and_fanout(
            second_bus,
            tenant_id="tenant-b",
            run_id="run-b",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="artifact-claim",
            trace_id="trace-b",
            payload={"text": "b" * 256},
        )
    assert fanout == ["artifact-claim", "artifact-claim"]
    assert len(store.payloads) == 1
    assert len(list(store.root.glob("*.json"))) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_artifact_materialization_failure_does_not_persist_event(tmp_path: Path) -> None:
    """claim 内 artifact 失败必须保持零 event，不能留下 dangling payload_ref。"""

    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    bus = EventBus(
        sink=sink,
        artifact_store=FailingArtifactStore(tmp_path / "artifacts"),
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    with pytest.raises(OSError, match="simulated artifact failure"):
        await bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="artifact-failure",
            trace_id="trace",
            payload={"text": "failure" * 64},
        )
    assert await sink.read(run_id="run") == []
    artifact_root = tmp_path / "artifacts"
    assert list(artifact_root.glob("*.json")) == []
    assert list((artifact_root / ".pending-artifact-claims").glob("*.json")) == []


@pytest.mark.asyncio
async def test_local_event_append_failure_compensates_new_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """artifact 已写成但 event 文件打不开时，不得留下无引用内容。"""

    event_path = tmp_path / "events.jsonl"
    store = FileArtifactStore(tmp_path / "artifacts")
    bus = EventBus(
        sink=LocalJsonlEventSink(event_path),
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    original_open = Path.open

    def fail_event_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == event_path and args and args[0] == "a":
            raise OSError("simulated event append open failure")
        return cast(IO[Any], original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fail_event_open)
    with pytest.raises(OSError, match="simulated event append open failure"):
        await bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="event-append-failure",
            trace_id="trace",
            payload={"text": "failure" * 64},
        )

    assert not event_path.exists()
    assert list(store.root.glob("*.json")) == []


@pytest.mark.asyncio
async def test_local_partial_event_write_failure_restores_file_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """event 只写入半行后失败时，同时恢复 JSONL 和本次 artifact。"""

    event_path = tmp_path / "events.jsonl"
    store = FileArtifactStore(tmp_path / "artifacts")
    sink = LocalJsonlEventSink(event_path)
    bus = EventBus(
        sink=sink,
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    baseline = await bus.publish(
        tenant_id="tenant",
        run_id="run",
        event_type=CanonicalEventType.RUN_STARTED,
        event_id="event-before-partial-write-failure",
        trace_id="trace",
        payload={"state": "before"},
    )
    original_open = Path.open

    class PartialWriteFailure:
        def __init__(self, file: IO[Any]) -> None:
            self._file = file

        def __enter__(self) -> PartialWriteFailure:
            self._file.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._file.__exit__(*args)

        def write(self, value: str) -> None:
            self._file.write(value[: len(value) // 2])
            self._file.flush()
            raise OSError("simulated partial event append failure")

        def flush(self) -> None:
            self._file.flush()

        def fileno(self) -> int:
            return self._file.fileno()

    def fail_partial_event_write(path: Path, *args: Any, **kwargs: Any) -> Any:
        file = cast(IO[Any], original_open(path, *args, **kwargs))
        if path == event_path and args and args[0] == "a":
            return PartialWriteFailure(file)
        return file

    monkeypatch.setattr(Path, "open", fail_partial_event_write)
    with pytest.raises(OSError, match="simulated partial event append failure"):
        await bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="partial-event-append-failure",
            trace_id="trace",
            payload={"text": "failure" * 64},
        )

    assert await sink.read(run_id="run") == [baseline]
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    assert list(store.root.glob("*.json")) == []


@pytest.mark.asyncio
async def test_local_event_fsync_failure_restores_file_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """event flush 后 fsync 失败也必须补偿本次 event 与 artifact。"""

    event_path = tmp_path / "events.jsonl"
    store = FileArtifactStore(tmp_path / "artifacts")
    sink = LocalJsonlEventSink(event_path)
    bus = EventBus(
        sink=sink,
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    baseline = await bus.publish(
        tenant_id="tenant",
        run_id="run",
        event_type=CanonicalEventType.RUN_STARTED,
        event_id="event-before-fsync-failure",
        trace_id="trace",
        payload={"state": "before"},
    )

    def fail_event_fsync(file: Any) -> None:
        raise OSError("simulated event fsync failure")

    monkeypatch.setattr(sink, "_fsync_event_file", fail_event_fsync)
    with pytest.raises(OSError, match="simulated event fsync failure"):
        await bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="event-fsync-failure",
            trace_id="trace",
            payload={"text": "failure" * 64},
        )

    assert await sink.read(run_id="run") == [baseline]
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    assert list(store.root.glob("*.json")) == []


@pytest.mark.asyncio
async def test_local_event_failure_never_deletes_preexisting_content_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补偿只删除本次新建内容，不能误删已存在的内容寻址证据。"""

    event_path = tmp_path / "events.jsonl"
    store = FileArtifactStore(tmp_path / "artifacts")
    payload = {"text": "shared" * 64}
    preexisting = store.write_json(payload)
    bus = EventBus(
        sink=LocalJsonlEventSink(event_path),
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    original_open = Path.open

    def fail_event_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == event_path and args and args[0] == "a":
            raise OSError("simulated event append open failure")
        return cast(IO[Any], original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fail_event_open)
    with pytest.raises(OSError, match="simulated event append open failure"):
        await bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="preexisting-artifact-event-failure",
            trace_id="trace",
            payload=payload,
        )

    assert not event_path.exists()
    assert store.read_json(preexisting.ref) == payload
    assert list(store.root.glob("*.json")) == [Path(preexisting.uri)]
