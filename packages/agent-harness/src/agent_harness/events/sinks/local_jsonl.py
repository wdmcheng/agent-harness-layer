"""local/offline profile 的 append-only JSONL 事件存储。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_harness.events.sinks.base import EventSink
from agent_harness.events.types import CanonicalEvent


class LocalJsonlEventSink(EventSink):
    """EventSink 的 append-only JSONL 实现。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def write(self, event: CanonicalEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event.to_payload(), ensure_ascii=False) + "\n")

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        if not self.path.exists():
            return events
        # JSONL 是 local mode 的简单证据存储。每次读取都重建 DTO，
        # 这样 contract test 能第一时间发现 envelope 漂移。
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            event = CanonicalEvent.model_validate(json.loads(line))
            if event.run_id == run_id and event.seq > after_seq:
                events.append(event)
        return events

    async def latest_seq(self, run_id: str) -> int:
        events = await self.read(run_id=run_id)
        if not events:
            return 0
        return max(event.seq for event in events)

    async def has_terminal(self, run_id: str) -> bool:
        return any(event.terminal for event in await self.read(run_id=run_id))
