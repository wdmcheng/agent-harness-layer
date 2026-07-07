"""不绑定 provider 的 CanonicalEvent 持久化协议。"""

from __future__ import annotations

from typing import Protocol

from agent_harness.events.types import CanonicalEvent


class EventSink(Protocol):
    """EventBus 需要的最小持久化 seam。

    runtime、API、CLI 和 eval 不应关心事件最终落到 JSONL、Postgres、Kafka、
    DBOS 还是托管 trace backend。协议越小，本地 smoke 和后续 service adapter
    越容易证明同一套排序和 terminal-event 语义。
    """

    async def write(self, event: CanonicalEvent) -> None:
        """持久化一个 CanonicalEvent。"""
        ...

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        """返回指定 run 在调用方已观察 seq 之后的事件。"""
        ...

    async def latest_seq(self, run_id: str) -> int:
        """返回指定 run 已持久化的最后一个 seq。"""
        ...

    async def has_terminal(self, run_id: str) -> bool:
        """报告指定 run 是否已经有 terminal event。"""
        ...
