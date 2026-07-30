"""供应商流事件的本地 shape doubles；合同测试不得直接依赖 SDK 包。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import agent_harness.adapters.models._pydantic_ai_streaming as pydantic_ai_streaming


@dataclass(frozen=True)
class AgentRunResultEvent:
    result: object


@dataclass(frozen=True)
class TextPart:
    content: str


@dataclass(frozen=True)
class ThinkingPart:
    content: str


@dataclass(frozen=True)
class TextPartDelta:
    content_delta: str


@dataclass(frozen=True)
class PartStartEvent:
    index: int
    part: object


@dataclass(frozen=True)
class PartDeltaEvent:
    index: int
    delta: object


@dataclass(frozen=True)
class PartEndEvent:
    index: int
    part: object


def patch_pydantic_stream_event_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """只替换 adapter 内部的 `isinstance` 类型，不让 SDK 类型越过边界。"""

    monkeypatch.setattr(pydantic_ai_streaming, "AgentRunResultEvent", AgentRunResultEvent)
    monkeypatch.setattr(pydantic_ai_streaming, "PartStartEvent", PartStartEvent)
    monkeypatch.setattr(pydantic_ai_streaming, "PartDeltaEvent", PartDeltaEvent)
    monkeypatch.setattr(pydantic_ai_streaming, "TextPart", TextPart)
    monkeypatch.setattr(pydantic_ai_streaming, "TextPartDelta", TextPartDelta)
