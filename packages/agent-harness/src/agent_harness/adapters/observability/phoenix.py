"""Phoenix observability adapter contract。"""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast

from agent_harness.adapters.observability.base import ClientTelemetryAdapter


class PhoenixTelemetryAdapter(ClientTelemetryAdapter):
    """Phoenix SDK 只在 adapter 默认 client 中延迟导入。"""

    provider_name = "phoenix"

    def _default_client(self) -> Any:
        phoenix_otel = cast(Any, import_module("phoenix.otel"))
        tracer_provider = phoenix_otel.register()
        return _PhoenixClient(tracer_provider)


class _PhoenixClient:
    def __init__(self, tracer_provider: Any) -> None:
        self._tracer_provider = tracer_provider

    def send(self, provider: str, payload: dict[str, Any]) -> None:
        tracer = self._tracer_provider.get_tracer("agent_harness")
        with tracer.start_as_current_span(payload["name"]) as span:
            span.set_attribute("provider", provider)
            span.set_attribute("agent_harness.payload_ref", payload.get("payload_ref") or "")
