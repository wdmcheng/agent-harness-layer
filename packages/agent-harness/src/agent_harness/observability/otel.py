"""CanonicalEvent 到 OTel 字段的稳定映射。"""

from __future__ import annotations

from typing import Any

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEvent
from agent_harness.observability.facade import (
    TelemetryRecord,
    prepare_telemetry_record,
)
from agent_harness.observability.redaction import redact_telemetry_payload


class OTelEventMapping(HarnessDTO):
    name: str
    attributes: dict[str, Any]


class OTelSpanMapping(HarnessDTO):
    name: str
    attributes: dict[str, Any]


class OTelMetricMapping(HarnessDTO):
    name: str
    value: int | float
    attributes: dict[str, Any]


class OTelRecordMapping(HarnessDTO):
    span: OTelSpanMapping
    event: OTelEventMapping
    metrics: list[OTelMetricMapping]


def map_event_to_otel(event: CanonicalEvent) -> OTelEventMapping:
    # OTel mapping 只输出稳定 envelope 字段。provider 原始细节留在
    # raw_event_ref/payload_ref 后面，不直接泄露进观测标签。
    return OTelEventMapping(
        name=f"agent_harness.{event.event_type.value}",
        attributes={
            "event_id": event.event_id,
            "tenant_id": event.tenant_id,
            "user_id": event.user_id,
            "run_id": event.run_id,
            "parent_run_id": event.parent_run_id,
            "agent_id": event.agent_id,
            "event_type": event.event_type.value,
            "seq": event.seq,
            "terminal": event.terminal,
            "trace_id": event.trace_id,
            "span_id": event.span_id,
        },
    )


class OTelTelemetryAdapter:
    """把 provider-neutral telemetry record 映射到 OTel span/metric/event DTO。

    真实 OpenTelemetry SDK/exporter 初始化留在 adapter/integration seam；这里不
    import exporter SDK，方便 core contract tests 在无 collector 的环境下验证字段。
    """

    provider_name = "otel"

    def map_record(self, record: TelemetryRecord) -> OTelRecordMapping:
        record = prepare_telemetry_record(record)
        attributes = _record_attributes(record)
        return OTelRecordMapping(
            span=OTelSpanMapping(name=record.name, attributes=attributes),
            event=OTelEventMapping(name=record.name, attributes=attributes),
            metrics=_record_metrics(record, attributes),
        )


def _record_attributes(record: TelemetryRecord) -> dict[str, Any]:
    context = record.context.to_payload()
    payload = redact_telemetry_payload(record.payload)
    payload_summary = {
        key: value for key, value in payload.items() if not isinstance(value, dict | list)
    }
    return {
        "telemetry.name": record.name,
        "telemetry.record_type": record.record_type,
        "payload_ref": record.payload_ref,
        "raw_event_ref": record.raw_event_ref,
        **context,
        **payload_summary,
    }


def _record_metrics(
    record: TelemetryRecord,
    attributes: dict[str, Any],
) -> list[OTelMetricMapping]:
    metric_keys = {
        "cost_usd",
        "count",
        "duration_ms",
        "input_tokens",
        "latency_ms",
        "output_tokens",
        "tokens",
    }
    metrics: list[OTelMetricMapping] = []
    for key, value in record.payload.items():
        if key in metric_keys and isinstance(value, int | float):
            metrics.append(
                OTelMetricMapping(
                    name=f"agent_harness.{key}",
                    value=value,
                    attributes=attributes,
                )
            )
    return metrics
