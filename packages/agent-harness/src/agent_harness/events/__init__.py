"""CanonicalEvent 与 EventBus 的公开 seam。"""

from agent_harness.events.bus import EventBus as EventBus
from agent_harness.events.bus import TerminalEventError as TerminalEventError
from agent_harness.events.serialization import (
    MAX_CANONICAL_EVENT_BYTES as MAX_CANONICAL_EVENT_BYTES,
)
from agent_harness.events.serialization import (
    CanonicalEventEnvelopeStateInvalid as CanonicalEventEnvelopeStateInvalid,
)
from agent_harness.events.serialization import (
    CanonicalEventEnvelopeTooLarge as CanonicalEventEnvelopeTooLarge,
)
from agent_harness.events.serialization import canonical_event_bytes as canonical_event_bytes
from agent_harness.events.serialization import canonical_json_bytes as canonical_json_bytes
from agent_harness.events.serialization import (
    validate_persisted_event_bytes as validate_persisted_event_bytes,
)
from agent_harness.events.sinks.base import EventSink as EventSink
from agent_harness.events.sinks.local_jsonl import LocalJsonlEventSink as LocalJsonlEventSink
from agent_harness.events.sinks.postgresql import PostgreSQLEventSink as PostgreSQLEventSink
from agent_harness.events.types import CanonicalEvent as CanonicalEvent
from agent_harness.events.types import CanonicalEventType as CanonicalEventType

_EVENT_MODEL_EXPORTS = [
    "CanonicalEvent",
    "CanonicalEventType",
]

_EVENT_BUS_EXPORTS = [
    "EventBus",
    "TerminalEventError",
]

_EVENT_SINK_EXPORTS = [
    "EventSink",
    "LocalJsonlEventSink",
    "PostgreSQLEventSink",
    "MAX_CANONICAL_EVENT_BYTES",
    "CanonicalEventEnvelopeTooLarge",
    "CanonicalEventEnvelopeStateInvalid",
    "canonical_event_bytes",
    "canonical_json_bytes",
    "validate_persisted_event_bytes",
]

__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
    *_EVENT_MODEL_EXPORTS,
    *_EVENT_BUS_EXPORTS,
    *_EVENT_SINK_EXPORTS,
]
