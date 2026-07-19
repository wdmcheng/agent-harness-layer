"""不绑定 provider 的 CanonicalEvent 持久化协议。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from agent_harness.contracts.run_trace import TRACE_ID_PATTERN, RunTraceValidationError
from agent_harness.events.types import CanonicalEvent, validate_terminal_semantics

DEFAULT_EVENT_PAGE_SIZE = 100
MAX_EVENT_PAGE_BYTES = 1_048_576


class EventSinkTerminalConflict(RuntimeError):
    """数据库级唯一约束拒绝同一 run 的第二个 terminal。"""


class EventSinkReplayConflict(ValueError):
    """event-id 已被不同事件语义占用，且不得向调用方泄露已有事件。"""


_REPLAY_SINK_ASSIGNED_FIELDS = {"seq", "timestamp"}


def validate_event_replay(incoming: CanonicalEvent, persisted: CanonicalEvent) -> None:
    """只允许同一稳定事件语义的 event-id 重放。

    Local JSONL 与 PostgreSQL 共用这一判断，避免 EventBus 预查询和 direct sink
    写入产生不同的幂等语义。``seq`` 由 sink 分配，``timestamp`` 在调用方重建
    同一重试时可能变化；除此之外 envelope 任一字段不同都表示另一逻辑事件。
    错误保持固定且不包含已有 envelope 或 payload。
    """

    incoming_fingerprint = incoming.model_dump(
        mode="json",
        exclude=_REPLAY_SINK_ASSIGNED_FIELDS,
    )
    persisted_fingerprint = persisted.model_dump(
        mode="json",
        exclude=_REPLAY_SINK_ASSIGNED_FIELDS,
    )
    if incoming_fingerprint != persisted_fingerprint:
        raise EventSinkReplayConflict("event replay envelope does not match persisted event")


def validate_terminal_visibility(event: CanonicalEvent) -> None:
    """DTO 被绕过时，direct sink 仍守住 terminal 的双向不变量。"""

    validate_terminal_semantics(
        event_type=event.event_type,
        terminal=event.terminal,
        visibility=event.visibility,
    )


def validate_event_scope(event: CanonicalEvent) -> None:
    """即使调用方绕过 DTO 构造，也在 direct sink seam 守住 typed scope。"""

    if event.record_scope not in {"run", "non_run"}:
        raise ValueError("record_scope must be run or non_run")
    if event.record_scope == "run" and (
        event.trace_id is None or TRACE_ID_PATTERN.fullmatch(event.trace_id) is None
    ):
        raise RunTraceValidationError


class EventSink(Protocol):
    """EventBus 需要的最小持久化 seam。

    runtime、API、CLI 和 eval 不应关心事件最终落到 JSONL、Postgres、Kafka、
    DBOS 还是托管 trace backend。协议越小，本地 smoke 和后续 service adapter
    越容易证明同一套排序和 terminal-event 语义。
    """

    async def write(self, event: CanonicalEvent) -> CanonicalEvent:
        """原子 claim event-id，持久化并返回 sink 实际分配 seq 的事件。

        同 ID 重试只能在 ``validate_event_replay`` 通过后返回已有事件；边界冲突
        必须在任何新增记录或下游副作用前失败。
        """
        ...

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        """返回指定 run 在调用方已观察 seq 之后的全部事件。

        这是保留给既有 RUN-003 等调用方的兼容 seam；新增流式入口应使用
        ``read_page``，避免一次把长 run 的全部未读 evidence 搬入内存。
        """
        ...

    async def latest_seq(self, run_id: str) -> int:
        """返回指定 run 已持久化的最后一个 seq。"""
        ...

    async def has_terminal(self, run_id: str) -> bool:
        """报告指定 run 是否已经有 terminal event。"""
        ...


class EventReader(Protocol):
    """RUN-006 与 CLI-EVT-001 复用的授权分页读取能力。

    该协议与 ``EventSink`` 分离，避免只负责写入与旧 JSON 读取的第三方 sink
    被迫实现流式 transport；service/local 的正式 event store 同时实现两者。
    """

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int = 0,
        include_internal: bool = False,
        max_events: int = DEFAULT_EVENT_PAGE_SIZE,
        max_bytes: int = MAX_EVENT_PAGE_BYTES,
    ) -> list[CanonicalEvent]:
        """按 exclusive cursor 返回受调用值与合同硬上限共同约束的一页。"""
        ...

    async def contains_seq(
        self,
        *,
        run_id: str,
        seq: int,
        include_internal: bool = False,
    ) -> bool:
        """只在当前可见性视图中判断 ``seq`` membership，供授权层复用。"""
        ...

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool = False,
    ) -> CanonicalEvent | None:
        """返回当前可见性视图中的唯一 terminal marker；不存在时返回 ``None``。"""
        ...


class ArtifactClaimEventSink(Protocol):
    """需要在 event claim 内 materialize artifact 的可选 sink 能力。"""

    async def write_after_claim(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None],
    ) -> CanonicalEvent:
        """claim 已锁定、事件未持久化时执行回调；失败必须保持零新增事件。"""
        ...


class CompensatingArtifactClaimEventSink(Protocol):
    """能把可补偿 artifact claim 包在 local event append 外层的 sink。"""

    async def write_with_artifact_claim(
        self,
        event: CanonicalEvent,
        *,
        artifact_claim: Callable[[Path, str, int], AbstractContextManager[None]],
    ) -> CanonicalEvent:
        """event claim 后进入 artifact 上下文；append 失败须触发其补偿。"""
        ...
