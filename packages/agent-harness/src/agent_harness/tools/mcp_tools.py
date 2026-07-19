"""把 MCP client 工具包装成 ToolRegistry 可执行工具。"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.mcp import MCPClient
from agent_harness.tools.output_guard import guarded_tool_payload
from agent_harness.tools.types import (
    ToolCallRequest,
    ToolCallResult,
    ToolError,
    ToolErrorCode,
    ToolRuntimeContext,
    tool_status_for_error,
)


class MCPTool:
    """单个 MCP tool 的受控调用包装。"""

    def __init__(
        self,
        *,
        server_name: str,
        name: str,
        input_schema: dict[str, Any],
        client: MCPClient,
        allowed: bool,
        artifact_store: FileArtifactStore,
        inline_result_bytes: int = 8192,
    ) -> None:
        """固定远端工具的身份、授权结果与大结果落盘边界。

        allowlist 在装配时计算为布尔值，避免一次调用期间因配置读取差异
        改变授权结论；结果大小阈值则交给 ``guarded_tool_payload`` 统一处理。
        """

        self.name = f"mcp.{server_name}.{name}"
        self.action = "mcp.connect"
        self.resource = f"mcp:{server_name}:{name}"
        self.input_schema = input_schema
        self._client = client
        self._remote_name = name
        self._allowed = allowed
        self._artifact_store = artifact_store
        self._inline_result_bytes = inline_result_bytes

    async def call(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        """调用远端 MCP 工具，并产出可审计、可截断的本地调用结果。

        拒绝的工具不会触网；允许的调用为每次请求生成独立 invocation ID，
        以便关联 artifact、事件证据和上游 trace。非字典响应会保留在
        ``content`` 字段中，避免因供应商返回形状差异丢失原始结果。
        """

        invocation_id = str(uuid4())
        source_ref = f"tool://{request.tool_name}/{context.run_id or 'adhoc'}/{invocation_id}"
        if not self._allowed:
            return ToolCallResult(
                tool_name=request.tool_name,
                status=tool_status_for_error(ToolErrorCode.ALLOWLIST_DENIED),
                invocation_id=invocation_id,
                error=ToolError(
                    code=ToolErrorCode.ALLOWLIST_DENIED,
                    message=f"MCP tool is not allowlisted: {self.name}",
                ),
                source_ref=source_ref,
                request_id=context.request_id or request.request_id,
                trace_id=context.trace_id or request.trace_id,
            )

        raw = await self._client.call_tool(self._remote_name, request.arguments)
        payload: dict[str, Any] = (
            cast(dict[str, Any], raw) if isinstance(raw, dict) else {"content": raw}
        )
        result, artifact_ref, truncation = guarded_tool_payload(
            tool_name=request.tool_name,
            invocation_id=invocation_id,
            payload=payload,
            artifact_store=self._artifact_store,
            inline_bytes=self._inline_result_bytes,
        )
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed",
            invocation_id=invocation_id,
            result=result,
            source_ref=source_ref,
            artifact_ref=artifact_ref,
            truncation=truncation,
            request_id=context.request_id or request.request_id,
            trace_id=context.trace_id or request.trace_id,
        )


async def mcp_tools_from_client(
    client: MCPClient,
    *,
    server_name: str,
    allowlist: list[str],
    artifact_store: FileArtifactStore,
    inline_result_bytes: int = 8192,
) -> list[MCPTool]:
    """读取 MCP server 工具，并按 allowlist 包装成可执行对象。"""

    descriptors = await client.list_tools()
    allowed = set(allowlist)
    return [
        MCPTool(
            server_name=server_name,
            name=descriptor.name,
            input_schema=descriptor.input_schema,
            client=client,
            allowed=descriptor.name in allowed or f"mcp.{server_name}.{descriptor.name}" in allowed,
            artifact_store=artifact_store,
            inline_result_bytes=inline_result_bytes,
        )
        for descriptor in descriptors
        if descriptor.server_name == server_name
    ]
