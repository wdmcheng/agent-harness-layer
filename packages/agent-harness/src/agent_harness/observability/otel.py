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
    """可发送给 OTel event 的名称与已净化属性集合。"""

    name: str
    attributes: dict[str, Any]


class OTelSpanMapping(HarnessDTO):
    """可创建为 OTel span 的名称与上下文属性集合。"""

    name: str
    attributes: dict[str, Any]


class OTelMetricMapping(HarnessDTO):
    """从受限 telemetry 字段派生的数值 metric，复用同一关联属性。"""

    name: str
    value: int | float
    attributes: dict[str, Any]


class OTelRecordMapping(HarnessDTO):
    """一条 provider-neutral telemetry 记录的 span、event 和 metrics 映射结果。"""

    span: OTelSpanMapping
    event: OTelEventMapping
    metrics: list[OTelMetricMapping]


def map_event_to_otel(event: CanonicalEvent) -> OTelEventMapping:
    """将 canonical event 的稳定 envelope 投影为 OTel event，不展开业务 payload。"""

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
        """先规范化和脱敏 telemetry 记录，再生成可导出的 OTel DTO 集合。"""

        record = prepare_telemetry_record(record)
        attributes = _record_attributes(record)
        return OTelRecordMapping(
            span=OTelSpanMapping(name=record.name, attributes=attributes),
            event=OTelEventMapping(name=record.name, attributes=attributes),
            metrics=_record_metrics(record, attributes),
        )


def _record_attributes(record: TelemetryRecord) -> dict[str, Any]:
    """组合关联上下文、引用与非嵌套净化字段，避免深层 payload 直接成为标签。"""

    context = record.context.to_payload()
    payload = redact_telemetry_payload(record.payload)
    # 嵌套对象往往承载 provider 原始响应；只导出标量摘要可控制 cardinality 与泄露面。
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
    """只从白名单数值字段派生 metric，避免任意 payload 键造成时序数据库爆炸。"""

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
