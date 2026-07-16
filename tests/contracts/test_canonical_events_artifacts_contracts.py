"""CanonicalEvent schema、本地序列与 replay 的公开契约测试。"""

from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.canonical_event_artifact_test_helpers import ContractRunTraceResolver

from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    TerminalEventError,
)


def _write_local_event_in_process(
    path: str,
    payload: dict[str, Any],
    start: Any,
    results: Any,
) -> None:
    """子进程从同一闸门竞争同一个 JSONL event-id。"""

    start.wait()
    try:
        event = asyncio.run(
            LocalJsonlEventSink(Path(path)).write(CanonicalEvent.model_validate(payload))
        )
        results.put(("ok", event.seq))
    except Exception as exc:  # pragma: no cover - 父进程会把异常转成断言证据
        results.put(("error", type(exc).__name__))


__all__ = [
    "Any",
    "CanonicalEvent",
    "CanonicalEventType",
    "ContractRunTraceResolver",
    "EventBus",
    "LocalJsonlEventSink",
    "Path",
    "TerminalEventError",
    "ValidationError",
    "_write_local_event_in_process",
    "asyncio",
    "cast",
    "multiprocessing",
    "pytest",
]
