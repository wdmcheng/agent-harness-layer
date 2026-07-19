"""Local CanonicalEvent/artifact 合同的可复用故障注入夹具。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.storage.run_trace_gate import RunTraceScopeConflict


class ContractRunTraceResolver:
    """纯 EventBus 合同显式声明授权 binding；持久化 resolver 由 trace 合同覆盖。"""

    def __init__(self, bindings: dict[tuple[str, str], str]) -> None:
        """固定测试允许的 tenant/run 到 trace 绑定，未声明组合必须显式失败。"""

        self._bindings = bindings

    async def __call__(self, *, tenant_id: str, run_id: str) -> str:
        """模拟异步 resolver；缺少绑定时抛出与真实 scope gate 相同的冲突类型。"""

        try:
            return self._bindings[(tenant_id, run_id)]
        except KeyError as exc:
            raise RunTraceScopeConflict from exc


class RecordingArtifactStore(FileArtifactStore):
    """记录 materialize 调用，区分纯 hash 预计算与真实 artifact 写入。"""

    def __init__(self, root: Path) -> None:
        """初始化真实 artifact 根目录及独立 payload 观察列表。"""

        super().__init__(root)
        self.payloads: list[dict[str, Any]] = []

    def write_json(self, payload: dict[str, Any]) -> Any:
        """先记录真正 materialize 的 payload，再委托父类维持内容寻址语义。"""

        self.payloads.append(payload)
        return super().write_json(payload)


class FailingArtifactStore(FileArtifactStore):
    """模拟 claim 后 materialize 失败，验证事件不得先于 artifact 落盘。"""

    def write_json(self, payload: dict[str, Any]) -> Any:
        """在 artifact 首写边界确定性失败，禁止被测 EventBus 产生半完成事件。"""

        raise OSError("simulated artifact failure")


class HardExitBeforeEventAppendSink(LocalJsonlEventSink):
    """子进程在 artifact 已可见、event 尚未 append 的窗口硬退出。"""

    def _append_event_unlocked(self, event: CanonicalEvent) -> None:
        """在 append 前直接退出子进程，保留 artifact 已可见的恢复窗口。"""

        os._exit(23)


class HardExitDuringPartialEventAppendSink(LocalJsonlEventSink):
    """写入半条 JSONL 后硬退出，验证按 journal offset 回滚。"""

    def _append_event_unlocked(self, event: CanonicalEvent) -> None:
        """仅写入半条 JSONL 后退出，验证下次启动按 journal offset 裁剪。"""

        with self.path.open("a", encoding="utf-8") as file:
            file.write('{"partial_event":')
            file.flush()
            os._exit(23)


class HardExitBeforePendingClearStore(FileArtifactStore):
    """event 已 fsync 后、pending journal 清理前硬退出。"""

    def _clear_pending_journal_unlocked(self, checksum: str) -> None:
        """在 event durable 后阻断 pending 清理，模拟提交已完成但清扫未完成。"""

        os._exit(23)


class HardExitBeforeArtifactVisibleStore(FileArtifactStore):
    """pending journal durable 后、artifact replace 前硬退出。"""

    def _write_json_unlocked(
        self,
        *,
        path: Path,
        checksum: str,
        data: bytes,
    ) -> Any:
        """在 replace artifact 前退出，保留仅有 pending journal 的可恢复状态。"""

        os._exit(23)


class FailOncePendingClearStore(FileArtifactStore):
    """第一次 journal clear 失败，验证下一受控操作可恢复。"""

    def __init__(self, root: Path) -> None:
        """初始化只失败一次的清理开关，以覆盖可重试 journal 收口路径。"""

        self._failed_clear = False
        super().__init__(root)

    def _clear_pending_journal_unlocked(self, checksum: str) -> None:
        """第一次清理注入失败，后续调用委托父类完成实际清扫。"""

        if not self._failed_clear:
            self._failed_clear = True
            raise OSError("simulated pending journal clear failure")
        super()._clear_pending_journal_unlocked(checksum)


def publish_large_event_then_hard_exit(event_path: str, artifact_root: str) -> None:
    """spawn-safe 硬退出入口；退出点由 sink 固定在 event append 之前。"""

    bus = EventBus(
        sink=HardExitBeforeEventAppendSink(Path(event_path)),
        artifact_store=FileArtifactStore(Path(artifact_root)),
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    asyncio.run(
        bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="hard-exit-before-event",
            trace_id="trace",
            payload={"text": "hard-exit" * 64},
        )
    )


def publish_large_event_then_exit_during_partial_append(
    event_path: str,
    artifact_root: str,
) -> None:
    """spawn-safe 半条 event 硬退出入口。"""

    bus = EventBus(
        sink=HardExitDuringPartialEventAppendSink(Path(event_path)),
        artifact_store=FileArtifactStore(Path(artifact_root)),
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    asyncio.run(
        bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="hard-exit-during-partial-event",
            trace_id="trace",
            payload={"text": "partial-hard-exit" * 64},
        )
    )


def publish_large_event_then_exit_before_journal_clear(
    event_path: str,
    artifact_root: str,
) -> None:
    """spawn-safe 已提交 event/未清 journal 硬退出入口。"""

    bus = EventBus(
        sink=LocalJsonlEventSink(Path(event_path)),
        artifact_store=HardExitBeforePendingClearStore(Path(artifact_root)),
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    asyncio.run(
        bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="hard-exit-before-journal-clear",
            trace_id="trace",
            payload={"text": "committed-hard-exit" * 64},
        )
    )


def publish_large_event_then_exit_before_artifact_visible(
    event_path: str,
    artifact_root: str,
) -> None:
    """spawn-safe journal 已提交但 artifact 尚不可见的退出入口。"""

    bus = EventBus(
        sink=LocalJsonlEventSink(Path(event_path)),
        artifact_store=HardExitBeforeArtifactVisibleStore(Path(artifact_root)),
        inline_payload_bytes=32,
        run_trace_resolver=ContractRunTraceResolver({("tenant", "run"): "trace"}),
    )
    asyncio.run(
        bus.publish(
            tenant_id="tenant",
            run_id="run",
            event_type=CanonicalEventType.ARTIFACT_CREATED,
            event_id="hard-exit-before-artifact",
            trace_id="trace",
            payload={"text": "journal-only-hard-exit" * 64},
        )
    )
