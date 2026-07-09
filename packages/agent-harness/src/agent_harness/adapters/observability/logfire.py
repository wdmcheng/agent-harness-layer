"""Logfire observability adapter contract。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from agent_harness.adapters.observability.base import ClientTelemetryAdapter


class LogfireTelemetryAdapter(ClientTelemetryAdapter):
    """Logfire SDK 只允许留在 adapter 默认 client 后面。"""

    provider_name = "logfire"

    def _default_client(self) -> Any:
        logfire = import_module("logfire")
        logfire.configure()
        return _LogfireClient(logfire)


class _LogfireClient:
    def __init__(self, module: Any) -> None:
        self._module = module

    def send(self, provider: str, payload: dict[str, Any]) -> None:
        self._module.info("agent_harness telemetry", provider=provider, payload=payload)
