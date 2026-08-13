"""只经受控文件工具读取工作区的仓库分析 Agent 示例。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol, cast

from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)
from agent_harness.tools import ToolCallRequest, ToolRegistry, ToolRuntimeContext
from agents.examples._shared import publish_example_trace
from agents.examples.repo_analyst.schemas import RepoAnalystInput, RepoAnalystOutput

_ALLOWED_TOOLS = ("file.read_file", "file.search_files", "file.list_files")


class _ToolRegistryFactory(Protocol):
    """运行时注入的工具注册表工厂协议，隔离模板与具体组合方式。"""

    def __call__(
        self,
        *,
        allowed_tools: Sequence[str],
        requested_tool_name: str,
    ) -> ToolRegistry:
        """按白名单和本次请求工具名构造受限注册表。"""
        ...


class RepoAnalystExecutor:
    """通过最小文件白名单完成仓库检查，不暴露 shell 执行能力。

    路径解析、租户与工作区边界由组合层配置的 ``WorkspacePolicy`` 负责；
    示例只选择工具，不能绕开注册表直接访问文件系统。
    """

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """选择一个只读工具并将结果压缩为适合 API 返回的摘要。

        无论工具返回成功或拒绝，都记录 trace 与引用地址，便于调用方在
        行内摘要被截断时回到 artifact/source 查看完整证据。
        """
        data = _input(request)
        tool_name, arguments = _tool_request(data)
        factory = cast(_ToolRegistryFactory, context.require_service("tool_registry_factory"))
        registry = factory(allowed_tools=_ALLOWED_TOOLS, requested_tool_name=tool_name)
        result = await registry.call(
            ToolCallRequest(
                tool_name=tool_name,
                arguments=arguments,
                agent_id=request.agent_id,
                run_id=request.run_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
            context=ToolRuntimeContext(
                actor=context.identity,
                agent_id=request.agent_id,
                run_id=request.run_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
        )
        error_code = None if result.error is None else result.error.code.value
        summary = _summary(result.result, error_code=error_code)
        trace = await publish_example_trace(
            context=context,
            request=request,
            name="examples.repo_analyst.tool_result",
            payload={
                "tool_name": tool_name,
                "status": result.status,
                "source_ref": result.source_ref,
                "artifact_ref": result.artifact_ref,
                "error_code": error_code,
                "trust_level": result.trust_level,
            },
        )
        output = RepoAnalystOutput(
            status=result.status,
            operation=data.operation,
            summary=summary,
            source_ref=result.source_ref,
            artifact_ref=result.artifact_ref,
            error_code=error_code,
            trust_level=result.trust_level,
            trace_ref=str(trace["trace_ref"]),
        )
        return AgentExecutionResult.completed(output.to_payload())

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """明确拒绝恢复，避免只读 Agent 被误接入审批 continuation。"""
        del request, context, grant
        return AgentExecutionResult.failed("repo analyst has no approval continuation")


def _input(request: AgentExecutionRequest) -> RepoAnalystInput:
    """将交互式 prompt 翻译为结构化只读操作，同时保留显式字段优先级。

    未带路径时使用当前工作区根目录，实际边界仍由底层文件工具校验。除
    交互式 ``prompt`` 外的未知字段交给严格 schema 拒绝，不能在此静默删除。
    """
    payload = dict(request.input)
    prompt = str(payload.pop("prompt", None) or "").strip()
    if prompt and "operation" not in payload:
        command, _, value = prompt.partition(" ")
        if command in {"read", "search", "list"}:
            payload["operation"] = command
            if command == "search":
                payload["query"] = value
            else:
                payload["path"] = value or "."
    return RepoAnalystInput.model_validate(payload)


def _tool_request(data: RepoAnalystInput) -> tuple[str, dict[str, str]]:
    """把经 schema 校验的操作映射为唯一允许的文件工具及参数。"""
    if data.operation == "search":
        return "file.search_files", {"query": data.query}
    if data.operation == "list":
        return "file.list_files", {"path": data.path}
    return "file.read_file", {"path": data.path}


def _summary(result: dict[str, object] | None, *, error_code: str | None) -> str:
    """生成有限长度的行内摘要，避免把大文件内容直接塞进 API 响应。

    拒绝结果优先暴露稳定错误码；没有行内载荷时提示调用方读取 artifact，
    以保留证据引用而不是把空结果误报为成功。
    """
    if error_code is not None:
        return f"workspace tool rejected the request: {error_code}"
    if result is None:
        return "workspace tool returned no inline payload; inspect artifact_ref"
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    return serialized[:500]


executor = RepoAnalystExecutor()
