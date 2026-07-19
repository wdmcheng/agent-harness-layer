"""CLI 工具子命令复用的 ToolRegistry 装配、授权与持久化证据边界。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.config import HarnessSettings
from agent_harness.config.schemas import MCPServerSettings
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.storage import (
    SQLAlchemyStorage,
    ToolInvocationCreate,
    WorkspaceCreate,
    require_migration_head,
    storage_dsn_from_settings,
)
from agent_harness.tools.file_tool import FileTool
from agent_harness.tools.registry import ToolRegistry
from agent_harness.tools.shell_tool import ShellTool
from agent_harness.tools.types import (
    BuiltinTool,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    ToolError,
    ToolErrorCode,
    ToolExecutionError,
    ToolRuntimeContext,
    tool_status_for_error,
)
from agent_harness.tools.workspace import WorkspacePolicy


async def configured_tool_names(settings: HarnessSettings) -> list[str]:
    """列出内置工具和配置中可发现的 MCP 工具名。"""

    names = [
        "file.read_file",
        "file.write_file",
        "file.list_files",
        "file.search_files",
        "file.apply_patch",
        "file.delete_file",
        "shell.execute",
    ]
    for server in settings.tools.mcp_servers:
        discovered = await discover_mcp_tool_names(server)
        names.extend(discovered)
    return sorted(set(names))


async def visible_tool_descriptors(
    *,
    settings: HarnessSettings,
    workspace_policy: WorkspacePolicy,
    artifact_store: FileArtifactStore,
    actor: IdentityContext,
    profiles_dir: Path | None,
) -> list[ToolDescriptor]:
    """通过 ToolRegistry 和 PolicyEngine 列出当前 actor/agent 可见工具。"""

    resolved_dsn = storage_dsn_from_settings(settings)
    require_migration_head(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    audit = AuditService(storage=storage)
    policy = _policy_engine(settings, storage, audit, profiles_dir)
    try:
        registry = build_tool_registry(
            settings=settings,
            workspace_policy=workspace_policy,
            artifact_store=artifact_store,
            policy=policy,
            audit=audit,
            requested_tool_name="",
        )
        visible: list[ToolDescriptor] = []
        for descriptor in registry.list_tools():
            decision = await policy.evaluate(
                PolicyCheck(
                    actor=actor,
                    action=descriptor.action,
                    resource=descriptor.resource,
                    context={
                        "tool_name": descriptor.name,
                        "agent_id": settings.agent.name,
                        "source": "cli",
                    },
                )
            )
            if decision.decision in {"allow", "require_approval"}:
                visible.append(descriptor.model_copy(update={"policy": decision.to_payload()}))
        return visible
    finally:
        await storage.dispose()


async def call_and_record_tool(
    request: ToolCallRequest,
    *,
    context: ToolRuntimeContext,
    workspace_policy: WorkspacePolicy,
    artifact_store: FileArtifactStore,
    settings: HarnessSettings,
    profiles_dir: Path | None,
    audit: AuditService | None = None,
    policy: PolicyEngine | None = None,
) -> ToolCallResult:
    """通过 ToolRegistry 调用工具，并写入 workspace/tool_invocation 证据。"""

    resolved_dsn = storage_dsn_from_settings(settings)
    require_migration_head(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    resolved_audit = audit or AuditService(storage=storage)
    resolved_policy = policy or _policy_engine(settings, storage, resolved_audit, profiles_dir)
    args_ref = artifact_store.write_json(
        {"tool_name": request.tool_name, "arguments": request.arguments}
    ).ref
    try:
        started = time.monotonic()
        registry = build_tool_registry(
            settings=settings,
            workspace_policy=workspace_policy,
            artifact_store=artifact_store,
            policy=resolved_policy,
            audit=resolved_audit,
            requested_tool_name=request.tool_name,
        )
        result = await registry.call(request, context=context)
        result = result.model_copy(
            update={
                "truncation": {
                    **result.truncation,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            }
        )
        result_ref = artifact_store.write_json(
            {"tool_name": request.tool_name, "result": result.to_payload()}
        ).ref
        await record_cli_tool_invocation(
            storage=storage,
            context=context,
            request=request,
            workspace_policy=workspace_policy,
            args_ref=args_ref,
            result_ref=result_ref,
            result=result,
        )
        return result
    finally:
        await storage.dispose()


def build_tool_registry(
    *,
    settings: HarnessSettings,
    workspace_policy: WorkspacePolicy,
    artifact_store: FileArtifactStore,
    policy: PolicyEngine,
    audit: AuditService,
    requested_tool_name: str,
) -> ToolRegistry:
    """创建 CLI/runtime 共用的 ToolRegistry。"""

    return ToolRegistry(
        tools=builtin_tools(
            settings=settings,
            workspace_policy=workspace_policy,
            artifact_store=artifact_store,
            policy=policy,
            requested_tool_name=requested_tool_name,
        ),
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        inline_result_bytes=settings.tools.workspace.inline_result_bytes,
        agent_tool_allowlist=_effective_agent_tool_allowlist(settings),
        enforce_agent_tool_allowlist=True,
    )


def builtin_tools(
    *,
    settings: HarnessSettings,
    workspace_policy: WorkspacePolicy,
    artifact_store: FileArtifactStore,
    policy: PolicyEngine,
    requested_tool_name: str,
) -> list[BuiltinTool]:
    """返回内置 File/Shell/MCP 工具描述。"""

    file_tool = FileTool(
        workspace_policy,
        artifact_store=artifact_store,
        policy=policy,
        inline_result_bytes=settings.tools.workspace.inline_result_bytes,
    )
    shell_tool = ShellTool(
        workspace=workspace_policy,
        artifact_store=artifact_store,
        enabled=settings.tools.shell.enabled,
        allowlist=settings.tools.shell.allowlist,
        denylist=settings.tools.shell.denylist,
        env_whitelist=settings.tools.shell.env_whitelist,
        timeout_seconds=settings.tools.shell.timeout_seconds,
        inline_output_bytes=settings.tools.shell.inline_output_bytes,
    )
    tools = [
        BuiltinTool(
            name="file.read_file",
            action="file.read",
            resource="workspace:file",
            input_schema=_schema_with_required("path"),
            handler=file_tool.read_file,
        ),
        BuiltinTool(
            name="file.write_file",
            action="file.bulk_write",
            resource="workspace:file",
            input_schema=_schema_with_required("path", "content"),
            handler=file_tool.write_file,
        ),
        BuiltinTool(
            name="file.list_files",
            action="file.list",
            resource="workspace:file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=file_tool.list_files,
        ),
        BuiltinTool(
            name="file.search_files",
            action="file.search",
            resource="workspace:file",
            input_schema=_schema_with_required("query"),
            handler=file_tool.search_files,
        ),
        BuiltinTool(
            name="file.apply_patch",
            action="file.bulk_write",
            resource="workspace:file",
            input_schema=_schema_with_required("path", "old", "new"),
            handler=file_tool.apply_patch,
        ),
        BuiltinTool(
            name="file.delete_file",
            action="file.delete",
            resource="workspace:file",
            input_schema=_schema_with_required("path"),
            handler=file_tool.delete_file,
        ),
        BuiltinTool(
            name="shell.execute",
            action="shell.execute",
            resource="shell:workspace",
            input_schema=_schema_with_required("command"),
            handler=shell_tool.execute,
            preflight=shell_tool.preflight,
        ),
    ]
    tools.extend(_mcp_builtin_tools(settings, requested_tool_name=requested_tool_name))
    return tools


async def discover_mcp_tool_names(server: MCPServerSettings) -> list[str]:
    """尝试真实 discovery；server 不可达时回退到 allowlist 名称。"""

    if server.command is None and server.url is None:
        return [f"mcp.{server.name}.{tool_name}" for tool_name in server.allowlist]
    try:
        from agent_harness.adapters.mcp.python_sdk import PythonMCPClient

        async with PythonMCPClient(
            server_name=server.name,
            transport=server.transport,
            command=server.command,
            args=server.args,
            url=server.url,
        ) as client:
            tools = await client.list_tools()
        return [f"mcp.{server.name}.{tool.name}" for tool in tools]
    except Exception:  # noqa: BLE001 - list 命令不能因单个 MCP server 不可达整体失败
        return [f"mcp.{server.name}.{tool_name}" for tool_name in server.allowlist]


async def record_cli_tool_invocation(
    *,
    storage: SQLAlchemyStorage,
    context: ToolRuntimeContext,
    request: ToolCallRequest,
    workspace_policy: WorkspacePolicy,
    args_ref: str,
    result_ref: str,
    result: ToolCallResult,
) -> None:
    """写入 tools CLI 的 workspace 和 tool_invocation evidence。"""

    duration_ms = result.truncation.get("duration_ms")
    if not isinstance(duration_ms, int):
        duration_ms = None
    async with storage.uow() as uow:
        await uow.tenants.ensure(context.actor.tenant_id)
        await uow.workspaces.create(
            WorkspaceCreate(
                tenant_id=context.actor.tenant_id,
                agent_id=context.agent_id,
                run_id=context.run_id,
                root_path=str(workspace_policy.root),
                policy_ref="policy:profile",
                metadata={"source": "cli"},
            )
        )
        await uow.tool_invocations.create(
            ToolInvocationCreate(
                tenant_id=context.actor.tenant_id,
                agent_id=context.agent_id,
                run_id=context.run_id,
                tool_name=request.tool_name,
                args_ref=args_ref,
                result_ref=result_ref,
                status=result.status,
                duration_ms=duration_ms,
                trace_id=context.trace_id or request.trace_id,
                request_id=context.request_id or request.request_id,
                metadata={
                    "invocation_id": result.invocation_id,
                    "source_ref": result.source_ref,
                    "artifact_ref": result.artifact_ref,
                    "trust_level": result.trust_level,
                    "error": None if result.error is None else result.error.to_payload(),
                },
            )
        )
        await uow.commit()


def _schema_with_required(*fields: str) -> dict[str, Any]:
    """构造最小对象 schema，供内置工具在进入 handler 前校验必填文本字段。"""
    return {
        "type": "object",
        "required": list(fields),
        "properties": {field: {"type": "string"} for field in fields},
    }


def _mcp_builtin_tools(
    settings: HarnessSettings,
    *,
    requested_tool_name: str,
) -> list[BuiltinTool]:
    """根据配置 allowlist 生成 MCP 工具描述，并显式表示本次越权请求。

    已配置工具可正常发现；若调用方指定同一服务下的未授权工具，则仍创建一个
    只会拒绝的描述符，让拒绝经统一 ToolRegistry、审计和错误信封返回，而不是
    因“找不到工具”绕过 allowlist 语义。
    """
    tools: list[BuiltinTool] = []
    for server in settings.tools.mcp_servers:
        allowlist = set(server.allowlist)
        for remote_name in sorted(allowlist):
            tools.append(_mcp_builtin_tool(server, remote_name, allowed=True))
        prefix = f"mcp.{server.name}."
        if requested_tool_name.startswith(prefix):
            remote_name = requested_tool_name.removeprefix(prefix)
            if remote_name not in allowlist:
                tools.append(_mcp_builtin_tool(server, remote_name, allowed=False))
    return tools


def _mcp_builtin_tool(server: MCPServerSettings, remote_name: str, *, allowed: bool) -> BuiltinTool:
    """创建单个 MCP 工具的受控包装，统一预检、连接与返回值投影。

    ``allowed`` 为假时预检和 handler 都会拒绝，双层防护避免调用方绕过预检后
    仍触达远端服务；为真时才延迟导入 SDK 并建立本次调用的客户端会话。
    """

    def _preflight(
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult | None:
        """在实际连接 MCP 前拒绝未列入 allowlist 的工具请求。"""
        if allowed:
            return None
        invocation_id = str(uuid4())
        return ToolCallResult(
            tool_name=request.tool_name,
            status=tool_status_for_error(ToolErrorCode.ALLOWLIST_DENIED),
            invocation_id=invocation_id,
            error=ToolError(
                code=ToolErrorCode.ALLOWLIST_DENIED,
                message=f"MCP tool is not allowlisted: mcp.{server.name}.{remote_name}",
            ),
            source_ref=f"tool://{request.tool_name}/{context.run_id or 'adhoc'}/{invocation_id}",
            request_id=context.request_id or request.request_id,
            trace_id=context.trace_id or request.trace_id,
        )

    async def _handler(arguments: dict[str, Any]) -> dict[str, Any]:
        """在授权通过后调用远端 MCP 工具，并投影为注册表约定的对象载荷。"""
        if not allowed:
            raise ToolExecutionError(
                ToolErrorCode.ALLOWLIST_DENIED,
                f"MCP tool is not allowlisted: mcp.{server.name}.{remote_name}",
            )
        from agent_harness.adapters.mcp.python_sdk import PythonMCPClient

        async with PythonMCPClient(
            server_name=server.name,
            transport=server.transport,
            command=server.command,
            args=server.args,
            url=server.url,
        ) as client:
            raw_result = await client.call_tool(remote_name, arguments)
        return _mcp_payload(raw_result)

    return BuiltinTool(
        name=f"mcp.{server.name}.{remote_name}",
        action="mcp.connect",
        resource=f"mcp:{server.name}:{remote_name}",
        input_schema={"type": "object"},
        handler=_handler,
        preflight=_preflight,
    )


def _mcp_payload(raw_result: Any) -> dict[str, Any]:
    """把不同 SDK 返回形态收敛为 JSON 对象，保留无法结构化时的可审计文本。"""
    if isinstance(raw_result, dict):
        return cast(dict[str, Any], raw_result)
    if hasattr(raw_result, "to_payload"):
        payload = raw_result.to_payload()
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
    if hasattr(raw_result, "model_dump"):
        payload = raw_result.model_dump(mode="json")
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
    return {"content": str(raw_result)}


def _effective_agent_tool_allowlist(settings: HarnessSettings) -> list[str]:
    """返回 Agent 显式声明的工具权限；空列表表示没有 CLI 调试例外。"""
    # 空 agent.tool_allowlist 表示没有工具权限；CLI 调试能力也必须由 profile
    # 或 agent config 显式声明，不能在 runtime seam 隐式扩权。
    return list(settings.agent.tool_allowlist)


def _policy_engine(
    settings: HarnessSettings,
    storage: SQLAlchemyStorage,
    audit: AuditService,
    profiles_dir: Path | None,
) -> PolicyEngine:
    """复用 CLI 共享策略装配，避免工具命令与主 CLI 解析不同的 profile 规则。"""
    from agent_harness.cli_shared import policy_engine

    return policy_engine(settings, storage, audit, profiles_dir=profiles_dir)
