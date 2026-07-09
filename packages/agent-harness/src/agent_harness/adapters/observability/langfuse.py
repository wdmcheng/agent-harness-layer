"""Langfuse observability adapter contract。"""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast

from agent_harness.adapters.observability.base import ClientTelemetryAdapter


class LangfuseTelemetryAdapter(ClientTelemetryAdapter):
    """Langfuse v4 SDK 只在 adapter 默认 client 中延迟导入。"""

    provider_name = "langfuse"

    def _default_client(self) -> Any:
        langfuse = cast(Any, import_module("langfuse"))
        return _LangfuseClient(langfuse.get_client())


class _LangfuseClient:
    def __init__(self, client: Any) -> None:
        self._client = client

    def send(self, provider: str, payload: dict[str, Any]) -> None:
        if hasattr(self._client, "create_event"):
            self._client.create_event(name=payload["name"], metadata=payload)
        elif hasattr(self._client, "update_current_trace"):
            self._client.update_current_trace(metadata={"provider": provider, **payload})
