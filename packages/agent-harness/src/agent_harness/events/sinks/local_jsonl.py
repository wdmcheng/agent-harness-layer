"""local/offline profile 的 append-only JSONL 事件存储。"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import TextIO

from agent_harness.events.sinks.base import (
    EventSink,
    EventSinkTerminalConflict,
    validate_event_replay,
    validate_event_scope,
    validate_terminal_visibility,
)
from agent_harness.events.types import CanonicalEvent
from agent_harness.local_state import register_local_state_file
from agent_harness.storage.run_trace_gate import RunTraceResolver, RunTraceScopeConflict


class LocalJsonlEventSink(EventSink):
    """EventSink 的 append-only JSONL 实现。"""

    def __init__(
        self,
        path: Path,
        *,
        state_dir: Path | None = None,
        run_trace_resolver: RunTraceResolver | None = None,
    ) -> None:
        self.path = path
        self.state_dir = state_dir
        self._run_trace_resolver = run_trace_resolver

    @property
    def run_trace_resolver(self) -> RunTraceResolver | None:
        """让共享同一 sink 的多个 EventBus 继承同一持久化门禁。"""

        return self._run_trace_resolver

    def bind_run_trace_resolver(self, resolver: RunTraceResolver) -> None:
        """由 composition root 注入持久化 resolver；禁止后续静默替换。"""

        if self._run_trace_resolver is not None and self._run_trace_resolver != resolver:
            raise RuntimeError("local event sink run trace resolver is already configured")
        self._run_trace_resolver = resolver

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        return await self._write_claimed(event, after_claim=after_claim)

    async def write_with_artifact_claim(
        self,
        event: CanonicalEvent,
        *,
        artifact_claim: Callable[[Path, str, int], AbstractContextManager[None]],
    ) -> CanonicalEvent:
        """把可补偿 artifact claim 包在 event append 外层。"""

        return await self._write_claimed(event, artifact_claim=artifact_claim)

    async def _write_claimed(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
        artifact_claim: Callable[[Path, str, int], AbstractContextManager[None]] | None = None,
    ) -> CanonicalEvent:
        validate_event_scope(event)
        validate_terminal_visibility(event)
        if event.record_scope == "run":
            if self._run_trace_resolver is None:
                raise RuntimeError("run-scoped local event sink requires a trace resolver")
            canonical = await self._run_trace_resolver(
                tenant_id=event.tenant_id,
                run_id=event.run_id,
            )
            if event.trace_id != canonical:
                raise RunTraceScopeConflict
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 锁覆盖“查 event-id / 查 terminal / 分配 seq / append”整个临界区。
        # 这样多个 EventBus、多个 sink 实例和多个进程不会把同一 retry 追加两次。
        with self._file_lock():
            register_local_state_file(self.path, kind="events", state_dir=self.state_dir)
            events = self._load_events_unlocked()
            existing = next((item for item in events if item.event_id == event.event_id), None)
            if existing is not None:
                validate_event_replay(event, existing)
                return existing
            if event.terminal and any(
                item.run_id == event.run_id and item.terminal for item in events
            ):
                raise EventSinkTerminalConflict
            persisted = event.model_copy(
                update={
                    "seq": max(
                        (item.seq for item in events if item.run_id == event.run_id),
                        default=0,
                    )
                    + 1
                }
            )
            event_size_before = self.path.stat().st_size if self.path.exists() else 0
            claim_context = (
                artifact_claim(self.path, event.event_id, event_size_before)
                if artifact_claim is not None
                else nullcontext()
            )
            with claim_context:
                if after_claim is not None:
                    after_claim()
                self._append_event_unlocked(persisted)
            return persisted

    async def write_after_claim(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None],
    ) -> CanonicalEvent:
        """把 artifact materialize 纳入同一跨进程 event claim 临界区。"""

        return await self.write(event, after_claim=after_claim)

    def _append_event_unlocked(self, event: CanonicalEvent) -> None:
        """append 失败时恢复原长度，避免暴露半条或未 fsync 的 event。"""

        existed = self.path.exists()
        original_size = self.path.stat().st_size if existed else 0
        try:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event.to_payload(), ensure_ascii=False) + "\n")
                file.flush()
                self._fsync_event_file(file)
            if not existed:
                self._fsync_directory(self.path.parent)
        except BaseException:
            self._restore_after_failed_append(existed=existed, original_size=original_size)
            raise

    def _restore_after_failed_append(self, *, existed: bool, original_size: int) -> None:
        """文件锁内补偿失败 append；不得改动先前已经提交的 JSONL 前缀。"""

        if not self.path.exists():
            return
        if not existed:
            self.path.unlink()
            return
        if self.path.stat().st_size == original_size:
            return
        with self.path.open("r+b") as file:
            file.truncate(original_size)
            file.flush()
            os.fsync(file.fileno())

    @staticmethod
    def _fsync_event_file(file: TextIO) -> None:
        """独立 seam 让合同测试精确注入 event fsync 失败。"""

        os.fsync(file.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """首次创建 JSONL 后同步父目录，确保恢复判断依赖的路径 durable。"""

        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        if not self.path.exists():
            return []
        # JSONL 是 local mode 的简单证据存储。每次读取都重建 DTO，
        # 这样 contract test 能第一时间发现 envelope 漂移。
        with self._file_lock():
            return [
                event
                for event in self._load_events_unlocked()
                if event.run_id == run_id and event.seq > after_seq
            ]

    async def latest_seq(self, run_id: str) -> int:
        events = await self.read(run_id=run_id)
        if not events:
            return 0
        return max(event.seq for event in events)

    async def has_terminal(self, run_id: str) -> bool:
        return any(event.terminal for event in await self.read(run_id=run_id))

    def _load_events_unlocked(self) -> list[CanonicalEvent]:
        if not self.path.exists():
            return []
        return [
            CanonicalEvent.model_validate(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    @contextmanager
    def _file_lock(self) -> Generator[None, None, None]:
        """用同路径 lock file 提供跨实例、跨进程的 append 临界区。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with lock_path.open("a+b") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
