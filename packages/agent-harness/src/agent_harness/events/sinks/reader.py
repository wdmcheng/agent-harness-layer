"""EventSink 流式读取共用的有界分页规则。"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_harness.events.serialization import validate_persisted_event_bytes
from agent_harness.events.sinks.base import DEFAULT_EVENT_PAGE_SIZE, MAX_EVENT_PAGE_BYTES
from agent_harness.events.types import CanonicalEvent


def _empty_event_page() -> list[CanonicalEvent]:
    """为 accumulator 创建独立事件容器，避免 dataclass 实例共享页面状态。"""

    return []


def validate_page_limits(*, max_events: int, max_bytes: int) -> None:
    """拒绝绕过合同硬上限或制造无法容纳合法单条 event 的页面。"""

    if not 1 <= max_events <= DEFAULT_EVENT_PAGE_SIZE:
        raise ValueError(f"max_events must be between 1 and {DEFAULT_EVENT_PAGE_SIZE}")
    if not 1 <= max_bytes <= MAX_EVENT_PAGE_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_EVENT_PAGE_BYTES}")


@dataclass
class EventPageAccumulator:
    """逐行/逐 row 组页，调用方无需先物化候选集合。"""

    max_events: int
    max_bytes: int
    events: list[CanonicalEvent] = field(default_factory=_empty_event_page, init=False)
    byte_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """在开始逐行读取前校验边界，避免后续读取路径出现不可判定的分页行为。"""

        validate_page_limits(max_events=self.max_events, max_bytes=self.max_bytes)

    def append(self, event: CanonicalEvent) -> bool:
        """验证并尝试加入一个已按授权可见性筛选的 row；False 表示页已满。"""

        encoded = validate_persisted_event_bytes(event)
        if not self.events and len(encoded) > self.max_bytes:
            raise ValueError("max_bytes is smaller than the next canonical event")
        if self.events and self.byte_count + len(encoded) > self.max_bytes:
            return False
        self.events.append(event)
        self.byte_count += len(encoded)
        return len(self.events) < self.max_events and self.byte_count < self.max_bytes


__all__ = ["EventPageAccumulator", "validate_page_limits"]
