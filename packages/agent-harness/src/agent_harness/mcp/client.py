"""MCP client 的 vendor-neutral 协议和测试 fake。"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class MCPToolDescriptor(HarnessDTO):
    """单个 MCP server 暴露的工具描述。"""

    server_name: str
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class MCPClient(Protocol):
    """核心包只依赖这个协议；官方 SDK 藏在 adapter 后面。"""

    async def list_tools(self) -> list[MCPToolDescriptor]:
        """返回当前 server 可调用工具的规范化描述，不暴露 SDK 私有类型。"""

        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """按工具名执行调用；具体传输、错误映射由 adapter 负责。"""

        ...


class FakeMCPClient:
    """合同测试使用的确定性 MCP client。"""

    def __init__(
        self,
        *,
        tools: list[MCPToolDescriptor],
        responses: dict[str, Any] | None = None,
    ) -> None:
        """注入固定工具清单和按名称分派的响应，构造无网络依赖的测试 double。"""

        self._tools = tools
        self._responses = responses or {}

    async def list_tools(self) -> list[MCPToolDescriptor]:
        """返回新列表，避免测试调用方改变 fake 内部工具顺序。"""

        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """返回预设响应；可调用响应接收原参数以验证调用契约。"""

        response = self._responses.get(name)
        if callable(response):
            return response(arguments)
        return response
