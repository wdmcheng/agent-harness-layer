"""Logfire/Phoenix/Langfuse adapter 共用的 provider-neutral 转换。"""

from __future__ import annotations

from typing import Any

from agent_harness.observability import TelemetryRecord, TelemetryStatus
from agent_harness.observability.facade import prepare_telemetry_record
from agent_harness.observability.redaction import redact_telemetry_payload


class ClientTelemetryAdapter:
    """把 TelemetryRecord 发给一个轻量 client seam。

    默认真实 SDK 入口由子类延迟创建；contract tests 可传 fake client，避免需要
    SaaS key，也避免 provider SDK object 穿过公共边界。
    """

    provider_name: str

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        return self.send_sync(record)

    def send_sync(self, record: TelemetryRecord) -> TelemetryStatus:
        payload = self.to_provider_payload(record)
        client = self._client or self._default_client()
        send = getattr(client, "send", None)
        if callable(send):
            send(self.provider_name, payload)
        return TelemetryStatus(provider=self.provider_name, status="sent")

    def to_provider_payload(self, record: TelemetryRecord) -> dict[str, Any]:
        record = prepare_telemetry_record(record)
        return redact_telemetry_payload(
            {
                "provider": self.provider_name,
                "name": record.name,
                "record_type": record.record_type,
                "context": record.context.to_payload(),
                "payload": record.payload,
                "payload_ref": record.payload_ref,
                "raw_event_ref": record.raw_event_ref,
            }
        )

    def _default_client(self) -> Any:
        raise RuntimeError(f"{self.provider_name} client is not configured")
