"""官方 MCP Python SDK adapter。

核心包只暴露 `agent_harness.mcp.MCPClient` 协议；这里是唯一允许 import
`mcp` SDK 的位置。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from agent_harness.mcp import MCPToolDescriptor


class PythonMCPClient:
    """基于官方 SDK 的 MCP client adapter，支持 stdio 和 streamable_http。"""

    def __init__(
        self,
        *,
        server_name: str,
        transport: str,
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
    ) -> None:
        """保存传输配置但不建立连接，确保 SDK 网络副作用只发生在异步上下文内。"""
        self._server_name = server_name
        self._transport = transport
        self._command = command
        self._args = args or []
        self._url = url
        self._exit_stack: AsyncExitStack | None = None
        self._session: Any | None = None

    async def __aenter__(self) -> PythonMCPClient:
        """建立传输与 ClientSession，并完成协议初始化后才暴露可调用客户端。"""
        self._exit_stack = AsyncExitStack()
        read_stream, write_stream = await self._connect_transport(self._exit_stack)
        from mcp import ClientSession

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """无论调用结果如何都关闭所有 SDK 资源，并清空会话防止退出后复用。"""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None

    async def list_tools(self) -> list[MCPToolDescriptor]:
        """读取远端工具描述并投影为核心包 DTO，隔离 SDK 字段命名差异。"""
        session = self._require_session()
        response = await session.list_tools()
        return [
            MCPToolDescriptor(
                server_name=self._server_name,
                name=tool.name,
                description=getattr(tool, "description", None),
                input_schema=getattr(tool, "inputSchema", None) or {"type": "object"},
            )
            for tool in response.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """调用已初始化会话中的远端工具；授权和参数 schema 由上层注册表负责。"""
        session = self._require_session()
        return await session.call_tool(name, arguments)

    async def _connect_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """按配置创建 stdio、streamable HTTP 或 SSE 传输，并把资源登记到同一栈。

        传输必需参数在连接前验证，避免 SDK 因空 command/URL 给出不稳定异常；
        所有导入延迟到实际使用分支，以保持核心包不依赖 MCP SDK。
        """
        if self._transport == "stdio":
            if self._command is None:
                raise ValueError("stdio MCP transport requires command")
            from mcp.client.stdio import StdioServerParameters, stdio_client

            return await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=self._command, args=self._args))
            )
        if self._transport == "streamable_http":
            if self._url is None:
                raise ValueError("streamable_http MCP transport requires url")
            from mcp.client.streamable_http import streamable_http_client

            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(self._url)
            )
            return read_stream, write_stream
        if self._transport == "sse":
            if self._url is None:
                raise ValueError("sse MCP transport requires url")
            from mcp.client.sse import sse_client

            return await stack.enter_async_context(sse_client(self._url))
        raise ValueError(f"unsupported MCP transport: {self._transport}")

    def _require_session(self) -> Any:
        """返回已初始化的 SDK 会话；在上下文外使用时显式拒绝而非隐式重连。"""
        if self._session is None:
            raise RuntimeError("PythonMCPClient must be used as an async context manager")
        return self._session
