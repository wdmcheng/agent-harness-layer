"""TelemetryFacade：local-first 的观测证据 fan-out seam。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from pydantic import Field

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.contracts.run_trace import RunTraceValidationError
from agent_harness.events import CanonicalEvent, CanonicalEventType
from agent_harness.events.sinks.base import EventSink
from agent_harness.observability.context import TelemetryContext
from agent_harness.observability.redaction import redact_telemetry_payload

DEFAULT_INLINE_PAYLOAD_BYTES = 8192


class TelemetryRecord(HarnessDTO):
    """provider-neutral telemetry payload，adapter 不接触业务对象或 SDK object。"""

    name: str
    record_type: str = "event"
    context: TelemetryContext
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_ref: str | None = None
    raw_event_ref: str | None = None


class TelemetryStatus(HarnessDTO):
    """单个 local/provider sink 的发送结果。"""

    provider: str
    status: str
    detail: str | None = None


class TelemetryPublishResult(HarnessDTO):
    """一次 telemetry publish 的 local 和 provider 结果。"""

    local_status: TelemetryStatus
    provider_statuses: list[TelemetryStatus]


class ProviderTelemetryAdapter(Protocol):
    """外部观测 provider adapter 的最小 contract。"""

    provider_name: str

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        """发送已脱敏 telemetry record。"""
        ...


class TelemetryFacade:
    """先写 local evidence，再 fan-out 外部 provider。"""

    def __init__(
        self,
        *,
        local_sink: EventSink,
        providers: list[ProviderTelemetryAdapter] | None = None,
        artifact_store: FileArtifactStore | None = None,
        inline_payload_bytes: int = DEFAULT_INLINE_PAYLOAD_BYTES,
    ) -> None:
        self._local_sink = local_sink
        self._providers = providers or []
        self._artifact_store = artifact_store
        self._inline_payload_bytes = inline_payload_bytes

    async def publish_event(self, event: CanonicalEvent) -> TelemetryPublishResult:
        """把 CanonicalEvent 映射到稳定的 observability seam。"""

        record = TelemetryRecord(
            name=f"agent_harness.{event.event_type.value}",
            record_type=_record_type_for_event(event.event_type),
            context=TelemetryContext(
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                agent_id=event.agent_id,
                run_id=event.run_id,
                request_id=event.request_id,
                trace_id=event.trace_id,
                span_id=event.span_id,
            ),
            payload=event.payload or {},
            payload_ref=event.payload_ref,
            raw_event_ref=event.raw_event_ref,
        )
        return await self.publish_record(record, event_type=event.event_type)

    async def publish_record(
        self,
        record: TelemetryRecord,
        *,
        event_type: CanonicalEventType = CanonicalEventType.ARTIFACT_CREATED,
    ) -> TelemetryPublishResult:
        """发布 provider-neutral record，保证 provider 失败不影响 local evidence。"""

        redacted_record = prepare_telemetry_record(
            record,
            artifact_store=self._artifact_store,
            inline_payload_bytes=self._inline_payload_bytes,
        )
        local_status = await self._write_local(redacted_record, event_type=event_type)
        provider_statuses: list[TelemetryStatus] = []
        for provider in self._providers:
            try:
                provider_statuses.append(await provider.send(redacted_record))
            except Exception as exc:  # noqa: BLE001 - provider failure must degrade, not crash runs
                provider_statuses.append(
                    TelemetryStatus(
                        provider=provider.provider_name,
                        status="degraded",
                        detail=str(redact_telemetry_payload(str(exc))),
                    )
                )
        return TelemetryPublishResult(
            local_status=local_status,
            provider_statuses=provider_statuses,
        )

    async def _write_local(
        self,
        record: TelemetryRecord,
        *,
        event_type: CanonicalEventType,
    ) -> TelemetryStatus:
        if record.context.run_id is not None and record.context.trace_id is None:
            raise RunTraceValidationError
        run_id = record.context.run_id or record.context.trace_id or "telemetry"
        seq = await self._local_sink.latest_seq(run_id) + 1
        event = CanonicalEvent(
            tenant_id=record.context.tenant_id,
            run_id=run_id,
            user_id=record.context.user_id,
            agent_id=record.context.agent_id,
            event_type=event_type,
            seq=seq,
            payload={
                "telemetry": {
                    "name": record.name,
                    "record_type": record.record_type,
                    "context": record.context.to_payload(),
                    "payload": record.payload,
                    "payload_ref": record.payload_ref,
                }
            },
            raw_event_ref=record.raw_event_ref,
            request_id=record.context.request_id,
            trace_id=record.context.trace_id,
            record_scope="run" if record.context.run_id is not None else "non_run",
            span_id=record.context.span_id,
        )
        await self._local_sink.write(event)
        return TelemetryStatus(provider="local-jsonl", status="written")


def prepare_telemetry_record(
    record: TelemetryRecord,
    *,
    artifact_store: FileArtifactStore | None = None,
    inline_payload_bytes: int = DEFAULT_INLINE_PAYLOAD_BYTES,
) -> TelemetryRecord:
    """脱敏并引用化 telemetry record，避免 provider/local sink 写入大 payload。"""

    payload = redact_telemetry_payload(record.payload)
    payload, payload_ref = _externalize_large_payload(
        payload=payload,
        payload_ref=record.payload_ref,
        artifact_store=artifact_store,
        inline_payload_bytes=inline_payload_bytes,
    )
    return TelemetryRecord(
        name=record.name,
        record_type=record.record_type,
        context=TelemetryContext.model_validate(
            redact_telemetry_payload(record.context.to_payload())
        ),
        payload=payload,
        payload_ref=payload_ref,
        raw_event_ref=record.raw_event_ref,
    )


def _externalize_large_payload(
    *,
    payload: dict[str, Any],
    payload_ref: str | None,
    artifact_store: FileArtifactStore | None,
    inline_payload_bytes: int,
) -> tuple[dict[str, Any], str | None]:
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    if len(payload_bytes) <= inline_payload_bytes:
        return payload, payload_ref

    summary_payload = _safe_payload_summary(payload)
    if artifact_store is not None:
        artifact = artifact_store.write_json(payload)
        return (
            {
                **summary_payload,
                "artifact": {
                    "size_bytes": artifact.size_bytes,
                    "checksum_sha256": artifact.checksum_sha256,
                },
            },
            artifact.ref,
        )

    checksum = hashlib.sha256(payload_bytes).hexdigest()
    return (
        {
            **summary_payload,
            "payload_omitted": {
                "size_bytes": len(payload_bytes),
                "checksum_sha256": checksum,
                "reason": "artifact_store_not_configured",
            },
        },
        payload_ref or f"payload://sha256/{checksum}",
    )


def _safe_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, int | float | bool):
            summary[key] = value
        elif isinstance(value, str) and len(value.encode()) <= 256:
            summary[key] = value
    return summary


def _record_type_for_event(event_type: CanonicalEventType) -> str:
    if event_type in {
        CanonicalEventType.MODEL_USAGE_UPDATED,
        CanonicalEventType.EVAL_SCORE_RECORDED,
    }:
        return "metric"
    return "event"
