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
        """注入可选 client seam；为空时延迟交由子类创建真实 SDK client。"""

        self._client = client

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        """提供异步调用面，同时复用同步 SDK 适配逻辑以保持 provider 行为一致。"""

        return self.send_sync(record)

    def send_sync(self, record: TelemetryRecord) -> TelemetryStatus:
        """转换、脱敏并发送记录；没有 ``send`` 方法的轻量 client 仍视为已接收。"""

        payload = self.to_provider_payload(record)
        client = self._client or self._default_client()
        send = getattr(client, "send", None)
        if callable(send):
            send(self.provider_name, payload)
        return TelemetryStatus(provider=self.provider_name, status="sent")

    def to_provider_payload(self, record: TelemetryRecord) -> dict[str, Any]:
        """补齐统一 telemetry 字段并在离开核心边界前脱敏 provider 载荷。"""

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
        """要求具体 provider 子类显式提供 SDK client，避免基类隐式选择外部服务。"""

        raise RuntimeError(f"{self.provider_name} client is not configured")
