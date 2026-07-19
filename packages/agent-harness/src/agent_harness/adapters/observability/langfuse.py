"""Langfuse observability adapter contract。"""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast

from agent_harness.adapters.observability.base import ClientTelemetryAdapter


class LangfuseTelemetryAdapter(ClientTelemetryAdapter):
    """Langfuse v4 SDK 只在 adapter 默认 client 中延迟导入。"""

    provider_name = "langfuse"

    def _default_client(self) -> Any:
        """延迟导入 Langfuse v4 并获取当前 client，避免核心包产生 SDK 依赖。"""

        langfuse = cast(Any, import_module("langfuse"))
        return _LangfuseClient(langfuse.get_client())


class _LangfuseClient:
    """兼容 Langfuse 不同 client 表面，优先写独立事件并回退当前 trace 更新。"""

    def __init__(self, client: Any) -> None:
        """保存 SDK client；调用面通过特性探测兼容受支持的版本差异。"""

        self._client = client

    def send(self, provider: str, payload: dict[str, Any]) -> None:
        """发送已脱敏 telemetry；没有兼容方法时保持无副作用而由上层返回 sent。"""

        if hasattr(self._client, "create_event"):
            self._client.create_event(name=payload["name"], metadata=payload)
        elif hasattr(self._client, "update_current_trace"):
            self._client.update_current_trace(metadata={"provider": provider, **payload})
