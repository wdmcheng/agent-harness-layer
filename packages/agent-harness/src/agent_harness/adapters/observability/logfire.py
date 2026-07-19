"""Logfire observability adapter contract。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from agent_harness.adapters.observability.base import ClientTelemetryAdapter


class LogfireTelemetryAdapter(ClientTelemetryAdapter):
    """Logfire SDK 只允许留在 adapter 默认 client 后面。"""

    provider_name = "logfire"

    def _default_client(self) -> Any:
        """延迟导入并初始化 Logfire，使测试可用 fake client 而无需 SaaS 配置。"""

        logfire = import_module("logfire")
        logfire.configure()
        return _LogfireClient(logfire)


class _LogfireClient:
    """把通用 telemetry 调用收敛为 Logfire 的结构化 info 事件。"""

    def __init__(self, module: Any) -> None:
        """保存延迟导入的 SDK 模块，不让其类型穿过公共适配器接口。"""

        self._module = module

    def send(self, provider: str, payload: dict[str, Any]) -> None:
        """通过 SDK 记录已脱敏 payload；脱敏由基类在此调用前完成。"""

        self._module.info("agent_harness telemetry", provider=provider, payload=payload)
