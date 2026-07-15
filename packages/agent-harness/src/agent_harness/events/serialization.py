"""CanonicalEvent 的唯一 JSON 字节表示与 envelope 大小门禁。"""

from __future__ import annotations

import json
import math
from typing import Any

from agent_harness.events.types import CanonicalEvent

MAX_CANONICAL_EVENT_BYTES = 65_536


class CanonicalEventSerializationError(ValueError):
    """payload 无法安全序列化为 canonical JSON。"""

    code = "event.envelope_state_invalid"


class CanonicalEventEnvelopeTooLarge(ValueError):
    """完整 CanonicalEvent envelope 超过持久化硬上限。"""

    code = "event.envelope_too_large"


class CanonicalEventEnvelopeStateInvalid(ValueError):
    """历史或 direct-write envelope 不能按当前公共规则安全读取。"""

    code = "event.envelope_state_invalid"


def canonical_json_bytes(payload: Any) -> bytes:
    """以跨 local、PostgreSQL、SSE 和 CLI 唯一的 JSON 规则编码。"""

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalEventSerializationError("canonical event payload is not JSON-safe") from exc
    return encoded


def canonical_event_bytes(event: CanonicalEvent) -> bytes:
    """返回 event envelope bytes；超限或 NaN 在任何 seq 消耗前失败。"""

    encoded = canonical_json_bytes(event.to_payload())
    if len(encoded) > MAX_CANONICAL_EVENT_BYTES:
        raise CanonicalEventEnvelopeTooLarge(
            f"canonical event envelope exceeds {MAX_CANONICAL_EVENT_BYTES} bytes"
        )
    return encoded


def validate_persisted_event_bytes(event: CanonicalEvent) -> bytes:
    """读取已持久化 row 时把非法状态收敛为稳定 fail-closed 错误。"""

    try:
        return canonical_event_bytes(event)
    except (CanonicalEventEnvelopeTooLarge, CanonicalEventSerializationError) as exc:
        raise CanonicalEventEnvelopeStateInvalid(
            "persisted canonical event envelope is invalid"
        ) from exc


def assert_finite_number(value: object) -> None:
    """供事件边界复用的有限数值检查。"""

    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise CanonicalEventSerializationError("canonical event number must be finite")


__all__ = [
    "MAX_CANONICAL_EVENT_BYTES",
    "CanonicalEventEnvelopeTooLarge",
    "CanonicalEventEnvelopeStateInvalid",
    "CanonicalEventSerializationError",
    "canonical_event_bytes",
    "canonical_json_bytes",
    "validate_persisted_event_bytes",
]
