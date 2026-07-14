"""Local event/artifact pending journal 的崩溃恢复契约测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest
from tests.contracts.canonical_event_artifact_test_helpers import (
    ContractRunTraceResolver,
    FailOncePendingClearStore,
    publish_large_event_then_exit_before_artifact_visible,
    publish_large_event_then_exit_before_journal_clear,
    publish_large_event_then_exit_during_partial_append,
    publish_large_event_then_hard_exit,
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink


def test_local_hard_exit_before_event_append_recovers_orphan_artifact(tmp_path: Path) -> None:
    """exit 23 跨过异常补偿后，下次 store 启动仍必须删除 orphan。"""

    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_hard_exit,
        args=(str(event_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    assert not event_path.exists()
    assert len(list(artifact_root.glob("*.json"))) == 1
    pending_paths = list((artifact_root / ".pending-artifact-claims").glob("*.json"))
    assert len(pending_paths) == 1
    pending_text = pending_paths[0].read_text(encoding="utf-8")
    pending = json.loads(pending_text)
    assert set(pending) == {
        "checksum",
        "created",
        "event_id_sha256",
        "event_path_sha256",
        "event_size_before",
        "version",
    }
    assert pending["created"] is True
    assert pending["event_size_before"] == 0
    assert (
        pending["event_path_sha256"]
        == hashlib.sha256(str(event_path.resolve()).encode()).hexdigest()
    )
    assert "hard-exit-before-event" not in pending_text
    assert "hard-exit" * 8 not in pending_text

    FileArtifactStore(artifact_root)

    assert list(artifact_root.glob("*.json")) == []


def test_local_hard_exit_during_partial_event_append_restores_original_size(
    tmp_path: Path,
) -> None:
    """半条 JSONL 硬退出后按 journal offset 截断，并删除新 artifact。"""

    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_exit_during_partial_append,
        args=(str(event_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    assert event_path.stat().st_size > 0
    assert len(list(artifact_root.glob("*.json"))) == 1

    FileArtifactStore(artifact_root)

    assert event_path.stat().st_size == 0
    assert list(artifact_root.glob("*.json")) == []
    assert list((artifact_root / ".pending-artifact-claims").glob("*.json")) == []


def test_local_hard_exit_after_journal_before_artifact_clears_pending(tmp_path: Path) -> None:
    """journal 已 durable 但 artifact 尚不可见时，恢复只需清 pending。"""

    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_exit_before_artifact_visible,
        args=(str(event_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    assert not event_path.exists()
    assert list(artifact_root.glob("*.json")) == []
    pending_dir = artifact_root / ".pending-artifact-claims"
    assert len(list(pending_dir.glob("*.json"))) == 1

    FileArtifactStore(artifact_root)

    assert list(artifact_root.glob("*.json")) == []
    assert list(pending_dir.glob("*.json")) == []


def test_local_hard_exit_after_event_commit_preserves_referenced_artifact(tmp_path: Path) -> None:
    """event 已 fsync 时，恢复必须保留 artifact 并只清除 pending journal。"""

    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_exit_before_journal_clear,
        args=(str(event_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    artifact_paths = list(artifact_root.glob("*.json"))
    assert len(artifact_paths) == 1
    pending_dir = artifact_root / ".pending-artifact-claims"
    assert len(list(pending_dir.glob("*.json"))) == 1

    store = FileArtifactStore(artifact_root)

    event = CanonicalEvent.model_validate_json(event_path.read_text(encoding="utf-8"))
    assert event.payload_ref is not None
    assert store.read_json(event.payload_ref) == {"text": "committed-hard-exit" * 64}
    assert list(pending_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_next_controlled_publish_recovers_failed_pending_clear(tmp_path: Path) -> None:
    """同进程 journal clear 失败后，幂等重试先恢复再盲返已有 event。"""

    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    store = FailOncePendingClearStore(artifact_root)
    bus = EventBus(
        sink=LocalJsonlEventSink(event_path),
        artifact_store=store,
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )

    async def publish() -> CanonicalEvent:
        return await bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="retry-after-pending-clear-failure",
            trace_id="trace",
            payload={"text": "pending-clear" * 64},
        )

    with pytest.raises(OSError, match="simulated pending journal clear failure"):
        await publish()
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(list(artifact_root.glob("*.json"))) == 1
    assert len(list((artifact_root / ".pending-artifact-claims").glob("*.json"))) == 1

    recovered = await publish()

    assert recovered.event_id == "retry-after-pending-clear-failure"
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(list(artifact_root.glob("*.json"))) == 1
    assert list((artifact_root / ".pending-artifact-claims").glob("*.json")) == []


def test_local_hard_exit_never_deletes_preexisting_shared_artifact(tmp_path: Path) -> None:
    """created=false 的 pending 恢复不得删除其他 event 已持有的同内容 artifact。"""

    event_path = tmp_path / "failed-events.jsonl"
    artifact_root = tmp_path / "artifacts"
    payload = {"text": "hard-exit" * 64}
    preexisting = FileArtifactStore(artifact_root).write_json(payload)
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_hard_exit,
        args=(str(event_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    assert not event_path.exists()

    recovered = FileArtifactStore(artifact_root)

    assert recovered.read_json(preexisting.ref) == payload
    assert list(artifact_root.glob("*.json")) == [Path(preexisting.uri)]
    assert list((artifact_root / ".pending-artifact-claims").glob("*.json")) == []


def test_local_hard_exit_preserves_artifact_referenced_by_another_event_path(
    tmp_path: Path,
) -> None:
    """两个 event path 共享 checksum 时，失败 claim 不得破坏已提交引用。"""

    committed_path = tmp_path / "committed-events.jsonl"
    failed_path = tmp_path / "failed-events.jsonl"
    artifact_root = tmp_path / "artifacts"
    payload = {"text": "hard-exit" * 64}
    committed_store = FileArtifactStore(artifact_root)
    committed = asyncio.run(
        EventBus(
            sink=LocalJsonlEventSink(committed_path),
            artifact_store=committed_store,
            inline_payload_bytes=32,
            run_trace_resolver=ContractRunTraceResolver({("tenant", "run-a"): "trace-a"}),
        ).publish(
            tenant_id="tenant",
            run_id="run-a",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="committed-shared-artifact",
            trace_id="trace-a",
            payload=payload,
        )
    )
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_hard_exit,
        args=(str(failed_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23

    recovered = FileArtifactStore(artifact_root)

    assert committed.payload_ref is not None
    assert recovered.read_json(committed.payload_ref) == payload
    assert len(committed_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not failed_path.exists()
    assert len(list(artifact_root.glob("*.json"))) == 1


def test_next_process_recovers_old_checksum_pending_before_new_claim(tmp_path: Path) -> None:
    """旧进程硬退出后，新路径同 checksum claim 必须先恢复旧 pending。"""

    failed_path = tmp_path / "failed-events.jsonl"
    recovered_path = tmp_path / "recovered-events.jsonl"
    artifact_root = tmp_path / "artifacts"
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_hard_exit,
        args=(str(failed_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    assert len(list(artifact_root.glob("*.json"))) == 1

    recovered_store = FileArtifactStore(artifact_root)
    recovered = asyncio.run(
        EventBus(
            sink=LocalJsonlEventSink(recovered_path),
            artifact_store=recovered_store,
            inline_payload_bytes=32,
            run_trace_resolver=ContractRunTraceResolver({("tenant", "run-b"): "trace-b"}),
        ).publish(
            tenant_id="tenant",
            run_id="run-b",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="new-claim-after-hard-exit",
            trace_id="trace-b",
            payload={"text": "hard-exit" * 64},
        )
    )

    assert recovered.payload_ref is not None
    assert recovered_store.read_json(recovered.payload_ref) == {"text": "hard-exit" * 64}
    assert not failed_path.exists()
    assert len(recovered_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(list(artifact_root.glob("*.json"))) == 1
    assert list((artifact_root / ".pending-artifact-claims").glob("*.json")) == []


def test_pending_recovery_rejects_unregistered_event_path_without_mutation(tmp_path: Path) -> None:
    """被篡改为未知路径的 journal 必须 fail closed，不能删除任何 artifact。"""

    event_path = tmp_path / "events.jsonl"
    artifact_root = tmp_path / "artifacts"
    process = multiprocessing.get_context("spawn").Process(
        target=publish_large_event_then_hard_exit,
        args=(str(event_path), str(artifact_root)),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23
    pending_path = next((artifact_root / ".pending-artifact-claims").glob("*.json"))
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["event_path_sha256"] = hashlib.sha256(
        str((tmp_path / "untrusted" / "events.jsonl").resolve()).encode()
    ).hexdigest()
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    artifacts_before = list(artifact_root.glob("*.json"))

    with pytest.raises(RuntimeError, match="artifact pending claim recovery failed"):
        FileArtifactStore(artifact_root)

    assert list(artifact_root.glob("*.json")) == artifacts_before
    assert pending_path.exists()
    assert not (tmp_path / "untrusted").exists()
