"""local/offline profile 的 append-only JSONL 事件存储。"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import threading
from collections.abc import Awaitable, Callable, Generator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
    nullcontext,
)
from pathlib import Path
from typing import TextIO
from weakref import WeakKeyDictionary

from agent_harness.events.capacity import (
    LocalCapacityCommitUncertain,
    usage_capacity_binding,
)
from agent_harness.events.serialization import canonical_event_bytes, validate_persisted_event_bytes
from agent_harness.events.sinks.base import (
    DEFAULT_EVENT_PAGE_SIZE,
    MAX_EVENT_PAGE_BYTES,
    EventSink,
    EventSinkTerminalConflict,
    validate_event_replay,
    validate_event_scope,
    validate_terminal_visibility,
)
from agent_harness.events.sinks.reader import EventPageAccumulator, validate_page_limits
from agent_harness.events.types import CanonicalEvent
from agent_harness.local_state import register_local_state_file
from agent_harness.storage.event_capacity_repositories import EventSequenceStateInvalid
from agent_harness.storage.run_trace_gate import RunTraceResolver, RunTraceScopeConflict

CapacityClaim = Callable[[CanonicalEvent], AbstractAsyncContextManager[None]]
ArtifactClaim = Callable[[Path, str, int], AbstractContextManager[None]]
CapacityReconciler = Callable[[int], Awaitable[None]]

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[Path, asyncio.Lock],
] = WeakKeyDictionary()


def _path_lock_for_current_loop(path: Path) -> asyncio.Lock:
    """返回当前 event loop 内按规范路径共享的异步前置锁。

    ``flock`` 会阻塞调用线程，而容量 claim 会在持有文件锁时等待数据库 I/O。
    因此同一 loop 中所有 sink 实例必须先共享这一层锁，避免第二个实例阻塞
    event loop，令第一个实例无法恢复并释放跨进程文件锁。
    """

    loop = asyncio.get_running_loop()
    canonical_path = path.expanduser().resolve()
    with _PATH_LOCKS_GUARD:
        loop_locks = _PATH_LOCKS.setdefault(loop, {})
        return loop_locks.setdefault(canonical_path, asyncio.Lock())


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
        self._capacity_claim: CapacityClaim | None = None

    @property
    def manages_event_capacity(self) -> bool:
        """报告 direct sink 写入是否也受同一容量账本保护。"""

        return self._capacity_claim is not None

    @property
    def run_trace_resolver(self) -> RunTraceResolver | None:
        """让共享同一 sink 的多个 EventBus 继承同一持久化门禁。"""

        return self._run_trace_resolver

    def bind_run_trace_resolver(self, resolver: RunTraceResolver) -> None:
        """由 composition root 注入持久化 resolver；禁止后续静默替换。"""

        if self._run_trace_resolver is not None and self._run_trace_resolver != resolver:
            raise RuntimeError("local event sink run trace resolver is already configured")
        self._run_trace_resolver = resolver

    def bind_capacity_claim(self, capacity_claim: CapacityClaim) -> None:
        """绑定 local composition 的唯一容量 claim，禁止后续静默替换。"""

        if self._capacity_claim is not None and self._capacity_claim != capacity_claim:
            raise RuntimeError("local event sink capacity claim is already configured")
        self._capacity_claim = capacity_claim

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        return await self._write_claimed(
            event,
            after_claim=after_claim,
            capacity_claim=self._capacity_claim,
        )

    async def write_with_artifact_claim(
        self,
        event: CanonicalEvent,
        *,
        artifact_claim: Callable[[Path, str, int], AbstractContextManager[None]],
    ) -> CanonicalEvent:
        """把可补偿 artifact claim 包在 event append 外层。"""

        return await self._write_claimed(
            event,
            artifact_claim=artifact_claim,
            capacity_claim=self._capacity_claim,
        )

    async def write_with_capacity_claim(
        self,
        event: CanonicalEvent,
        *,
        capacity_claim: CapacityClaim,
        artifact_claim: ArtifactClaim | None = None,
    ) -> CanonicalEvent:
        """把 SQLite 账本提交与可补偿 JSONL append 纳入同一文件锁。"""

        return await self._write_claimed(
            event,
            capacity_claim=capacity_claim,
            artifact_claim=artifact_claim,
        )

    async def reconcile_capacity(
        self,
        run_id: str,
        *,
        reconcile: CapacityReconciler,
    ) -> None:
        """在 JSONL 文件锁内把已验证前缀交给 SQLite 账本接管。"""

        async with _path_lock_for_current_loop(self.path):
            with self._file_lock():
                events = self._load_events_unlocked()
                highest_persisted_seq = max(
                    (event.seq for event in events if event.run_id == run_id),
                    default=0,
                )
                await reconcile(highest_persisted_seq)

    async def _write_claimed(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
        artifact_claim: ArtifactClaim | None = None,
        capacity_claim: CapacityClaim | None = None,
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
        async with _path_lock_for_current_loop(self.path):
            with self._file_lock():
                register_local_state_file(self.path, kind="events", state_dir=self.state_dir)
                events = self._load_events_unlocked()
                usage_binding = usage_capacity_binding(event)
                if (
                    capacity_claim is not None
                    and usage_binding is not None
                    and usage_binding.phase == "final"
                ):
                    if not any(
                        item.event_id == usage_binding.started_event_id
                        and item.tenant_id == event.tenant_id
                        and item.run_id == event.run_id
                        for item in events
                    ):
                        raise RuntimeError("usage final event requires a persisted started event")
                existing = next(
                    (item for item in events if item.event_id == event.event_id),
                    None,
                )
                if existing is not None:
                    validate_event_replay(event, existing)
                    if capacity_claim is not None:
                        # JSONL append 已 fsync、SQLite commit 前硬退出时，重放命中
                        # event-id 仍须幂等推进容量账本；否则 outbox 会被公开为
                        # published，但 outstanding reservation 永久阻断 terminal。
                        async with capacity_claim(existing):
                            pass
                    return existing
                if any(item.run_id == event.run_id and item.terminal for item in events):
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
                event_existed_before = self.path.exists()
                event_size_before = self.path.stat().st_size if event_existed_before else 0
                claim_context = (
                    artifact_claim(self.path, event.event_id, event_size_before)
                    if artifact_claim is not None
                    else nullcontext()
                )
                with claim_context:
                    try:
                        if capacity_claim is None:
                            if after_claim is not None:
                                after_claim()
                            canonical_event_bytes(persisted)
                            self._append_event_unlocked(persisted)
                        else:
                            async with capacity_claim(persisted):
                                if after_claim is not None:
                                    after_claim()
                                canonical_event_bytes(persisted)
                                self._append_event_unlocked(persisted)
                    except BaseException as exc:
                        # capacity commit 发生在 append 之后；失败时先恢复 JSONL，随后
                        # artifact claim 才会看到异常并按“事件未提交”删除新 artifact。
                        if not isinstance(exc, LocalCapacityCommitUncertain):
                            self._restore_after_failed_append(
                                existed=event_existed_before,
                                original_size=event_size_before,
                            )
                        raise
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
                file.write(canonical_event_bytes(event).decode("utf-8") + "\n")
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
        async with _path_lock_for_current_loop(self.path):
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

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int = 0,
        include_internal: bool = False,
        max_events: int = DEFAULT_EVENT_PAGE_SIZE,
        max_bytes: int = MAX_EVENT_PAGE_BYTES,
    ) -> list[CanonicalEvent]:
        """只验证当前可见页，避免后续非法 row 被空页或预取掩盖。"""

        validate_page_limits(max_events=max_events, max_bytes=max_bytes)
        async with _path_lock_for_current_loop(self.path):
            if not self.path.exists():
                return []
            with self._file_lock():
                page = EventPageAccumulator(
                    max_events=max_events,
                    max_bytes=max_bytes,
                )
                # local JSONL 保持 append 顺序；逐行解析使 100-event 页不会把长
                # run 的全部未读 envelope 搬入内存，也不会触碰下一页的行。
                last_run_seq = 0
                with self.path.open("r", encoding="utf-8") as file:
                    for line in file:
                        if not line.strip():
                            continue
                        event = CanonicalEvent.model_validate(json.loads(line))
                        if event.run_id != run_id:
                            continue
                        if event.seq <= last_run_seq:
                            raise EventSequenceStateInvalid
                        last_run_seq = event.seq
                        if event.seq <= after_seq:
                            continue
                        # 这是等价于 PostgreSQL WHERE visibility 的授权视图筛选；
                        # 隐藏 row 不属于本 reader 的候选页，也不能形成错误 oracle。
                        if not include_internal and event.visibility != "public":
                            continue
                        if not page.append(event):
                            break
                return page.events

    async def contains_seq(
        self,
        *,
        run_id: str,
        seq: int,
        include_internal: bool = False,
    ) -> bool:
        if seq <= 0:
            return False
        page = await self.read_page(
            run_id=run_id,
            after_seq=seq - 1,
            include_internal=include_internal,
            max_events=1,
        )
        return bool(page and page[0].seq == seq)

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool = False,
    ) -> CanonicalEvent | None:
        async with _path_lock_for_current_loop(self.path):
            if not self.path.exists():
                return None
            with self._file_lock(), self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    event = CanonicalEvent.model_validate(json.loads(line))
                    if (
                        event.run_id == run_id
                        and event.terminal
                        and (include_internal or event.visibility == "public")
                    ):
                        validate_persisted_event_bytes(event)
                        return event
        return None

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
        events = [
            CanonicalEvent.model_validate(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        for event in events:
            validate_persisted_event_bytes(event)
        return events

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
