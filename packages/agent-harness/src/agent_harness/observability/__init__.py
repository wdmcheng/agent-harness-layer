"""观测映射与 provider adapter contract 的公开 seam。"""

from agent_harness.observability.context import TelemetryContext as TelemetryContext
from agent_harness.observability.facade import (
    ProviderTelemetryAdapter as ProviderTelemetryAdapter,
)
from agent_harness.observability.facade import TelemetryFacade as TelemetryFacade
from agent_harness.observability.facade import (
    TelemetryPublishResult as TelemetryPublishResult,
)
from agent_harness.observability.facade import TelemetryRecord as TelemetryRecord
from agent_harness.observability.facade import TelemetryStatus as TelemetryStatus
from agent_harness.observability.facade import (
    prepare_telemetry_record as prepare_telemetry_record,
)
from agent_harness.observability.otel import OTelEventMapping as OTelEventMapping
from agent_harness.observability.otel import OTelMetricMapping as OTelMetricMapping
from agent_harness.observability.otel import OTelRecordMapping as OTelRecordMapping
from agent_harness.observability.otel import OTelSpanMapping as OTelSpanMapping
from agent_harness.observability.otel import OTelTelemetryAdapter as OTelTelemetryAdapter
from agent_harness.observability.otel import map_event_to_otel as map_event_to_otel
from agent_harness.observability.redaction import (
    redact_telemetry_payload as redact_telemetry_payload,
)

_CONTEXT_EXPORTS = ["TelemetryContext"]

_FACADE_EXPORTS = [
    "ProviderTelemetryAdapter",
    "TelemetryFacade",
    "TelemetryPublishResult",
    "TelemetryRecord",
    "TelemetryStatus",
    "prepare_telemetry_record",
]

_OTEL_EXPORTS = [
    "OTelEventMapping",
    "OTelMetricMapping",
    "OTelRecordMapping",
    "OTelSpanMapping",
    "OTelTelemetryAdapter",
    "map_event_to_otel",
]

_REDACTION_EXPORTS = ["redact_telemetry_payload"]

__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
    *_CONTEXT_EXPORTS,
    *_FACADE_EXPORTS,
    *_OTEL_EXPORTS,
    *_REDACTION_EXPORTS,
]
