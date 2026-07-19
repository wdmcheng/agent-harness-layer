"""Phoenix observability adapter contract。"""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast

from agent_harness.adapters.observability.base import ClientTelemetryAdapter


class PhoenixTelemetryAdapter(ClientTelemetryAdapter):
    """Phoenix SDK 只在 adapter 默认 client 中延迟导入。"""

    provider_name = "phoenix"

    def _default_client(self) -> Any:
        """延迟导入并注册 Phoenix tracer，避免未启用 provider 时加载可选依赖。"""

        phoenix_otel = cast(Any, import_module("phoenix.otel"))
        tracer_provider = phoenix_otel.register()
        return _PhoenixClient(tracer_provider)


class _PhoenixClient:
    """将通用 telemetry payload 映射为 Phoenix span 的极小 SDK 包装。"""

    def __init__(self, tracer_provider: Any) -> None:
        """保存已注册 tracer provider；SDK 类型不离开本私有适配器。"""

        self._tracer_provider = tracer_provider

    def send(self, provider: str, payload: dict[str, Any]) -> None:
        """创建 span 并仅写入 provider 与 payload 引用，避免复制可能较大的内容。"""

        tracer = self._tracer_provider.get_tracer("agent_harness")
        with tracer.start_as_current_span(payload["name"]) as span:
            span.set_attribute("provider", provider)
            span.set_attribute("agent_harness.payload_ref", payload.get("payload_ref") or "")
