"""通过受控工具、策略预检与审批授权执行开发辅助操作。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from agent_harness.artifacts import FileArtifactStore
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)
from agent_harness.tools import (
    ToolCallRequest,
    ToolCallResult,
    ToolErrorCode,
    ToolRegistry,
    ToolRuntimeContext,
    hash_tool_arguments,
)
from agents.examples._shared import publish_example_trace
from agents.examples.dev_assistant.schemas import DevAssistantInput, DevAssistantOutput

_ALLOWED_TOOLS = (
    "file.read_file",
    "file.write_file",
    "file.list_files",
    "file.search_files",
    "file.apply_patch",
    "file.delete_file",
    "shell.execute",
)


class _ToolRegistryFactory(Protocol):
    """由组合层提供的注册表工厂，确保模板不绕过工具授权边界。"""

    def __call__(
        self,
        *,
        allowed_tools: Sequence[str],
        requested_tool_name: str,
    ) -> ToolRegistry:
        """为本次操作构造同时受白名单和策略约束的注册表。"""
        ...


class DevAssistantExecutor:
    """把开发动作分成策略预检与已授权恢复两条明确的安全路径。

    普通调用只能触发工具注册表的策略预检；需要审批时把不可变参数快照写入
    artifact，后续只有携带 ``ApprovalGrant`` 的 continuation 才能执行真实
    handler。这样调用者不能通过修改内存中的请求绕过已审计的授权内容。
    """

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """执行首次工具调用；遇到审批要求时持久化参数并返回等待状态。

        工具错误与未完成状态不会被包装成成功。审批分支同时保存原始参数和
        参数哈希，供授权服务绑定 continuation，避免路径或 shell 命令在
        人工确认后发生替换。
        """
        tool_request, registry = _tool_call(request, context=context)
        result = await registry.call(tool_request, context=_tool_context(request, context=context))
        if result.error is not None and result.error.code == ToolErrorCode.APPROVAL_REQUIRED:
            descriptor = next(
                item for item in registry.list_tools() if item.name == tool_request.tool_name
            )
            artifact_store = cast(
                FileArtifactStore,
                context.require_service("artifact_store"),
            )
            arguments_ref = artifact_store.write_json(
                {
                    "tool_name": tool_request.tool_name,
                    "arguments": tool_request.arguments,
                }
            ).ref
            return AgentExecutionResult.waiting(
                AgentApprovalRequest(
                    action=descriptor.action,
                    resource=descriptor.resource,
                    reason=result.error.message,
                    arguments_ref=arguments_ref,
                    arguments_hash=hash_tool_arguments(tool_request.arguments),
                    continuation={
                        "tool_name": tool_request.tool_name,
                        "arguments_ref": arguments_ref,
                    },
                )
            )
        if result.status != "completed":
            return AgentExecutionResult.failed(_error_message(result))
        return await _completed_result(request, context=context, result=result)

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """使用已验证的授权对象继续执行此前等待的工具请求。

        这里必须调用 ``call_approved`` 而不是普通 ``call``，由注册表重新
        校验授权与持久化参数绑定关系；失败仍保持失败语义，不生成完成输出。
        """
        tool_request, registry = _tool_call(request, context=context)
        result = await registry.call_approved(
            tool_request,
            context=_tool_context(request, context=context),
            grant=grant,
        )
        if result.status != "completed":
            return AgentExecutionResult.failed(_error_message(result))
        return await _completed_result(request, context=context, result=result)


def _tool_call(
    request: AgentExecutionRequest,
    *,
    context: AgentExecutionContext,
) -> tuple[ToolCallRequest, ToolRegistry]:
    """把已校验的开发意图映射为单个工具请求及其受限注册表。

    只暴露读取、写入与 shell 三种示例操作；具体路径、命令安全性与审批
    要求不在此处判断，统一交给注册表及其策略引擎，避免模板出现第二套
    不一致的授权逻辑。
    """
    data = _input(request)
    if data.operation == "write":
        tool_name = "file.write_file"
        arguments = {"path": data.path, "content": data.content}
    elif data.operation == "shell":
        tool_name = "shell.execute"
        arguments = {"command": data.command}
    else:
        tool_name = "file.read_file"
        arguments = {"path": data.path}
    factory = cast(_ToolRegistryFactory, context.require_service("tool_registry_factory"))
    registry = factory(allowed_tools=_ALLOWED_TOOLS, requested_tool_name=tool_name)
    return (
        ToolCallRequest(
            tool_name=tool_name,
            arguments=arguments,
            agent_id=request.agent_id,
            run_id=request.run_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        ),
        registry,
    )


def _tool_context(
    request: AgentExecutionRequest,
    *,
    context: AgentExecutionContext,
) -> ToolRuntimeContext:
    """把身份与调用链标识传给工具层，保证审计事件可关联到同一请求。"""
    return ToolRuntimeContext(
        actor=context.identity,
        agent_id=request.agent_id,
        run_id=request.run_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )


async def _completed_result(
    request: AgentExecutionRequest,
    *,
    context: AgentExecutionContext,
    result: ToolCallResult,
) -> AgentExecutionResult:
    """将工具完成结果、策略决定和引用地址写入 trace 后生成公共输出。

    ``decision`` 只在返回类型确为字符串时进入 API 载荷，避免策略扩展字段
    以不稳定结构泄漏给调用方。完整工具结果继续由 source/artifact 引用承载。
    """
    policy_decision = None
    raw_decision = result.policy.get("decision")
    if isinstance(raw_decision, str):
        policy_decision = raw_decision
    trace = await publish_example_trace(
        context=context,
        request=request,
        name="examples.dev_assistant.tool_completed",
        payload={
            "tool_name": result.tool_name,
            "status": result.status,
            "source_ref": result.source_ref,
            "artifact_ref": result.artifact_ref,
            "policy_decision": policy_decision,
        },
    )
    output = DevAssistantOutput(
        status=result.status,
        tool_name=result.tool_name,
        result=result.result,
        source_ref=result.source_ref,
        artifact_ref=result.artifact_ref,
        policy_decision=policy_decision,
        trace_ref=str(trace["trace_ref"]),
    )
    return AgentExecutionResult.completed(output.to_payload())


def _input(request: AgentExecutionRequest) -> DevAssistantInput:
    """兼容简短 prompt，并在 schema 校验前将其还原为结构化开发操作。

    显式 ``operation`` 优先于 prompt；``write`` 使用 ``路径::内容`` 的
    简写，仅用于示例交互。未提供分隔符时写入空内容，随后由 schema 和
    工具策略决定是否允许，不能在这里默默猜测目标文件。
    """
    payload = dict(request.input)
    payload.pop("source", None)
    prompt = str(payload.pop("prompt", None) or "").strip()
    if prompt and "operation" not in payload:
        command, _, value = prompt.partition(" ")
        if command == "read":
            payload.update({"operation": "read", "path": value or "README.md"})
        elif command == "shell":
            payload.update({"operation": "shell", "command": value})
        elif command == "write":
            path, separator, content = value.partition("::")
            payload.update(
                {
                    "operation": "write",
                    "path": path,
                    "content": content if separator else "",
                }
            )
    return DevAssistantInput.model_validate(payload)


def _error_message(result: ToolCallResult) -> str:
    """把工具失败归一为稳定、可展示的错误文本。"""
    if result.error is None:
        return f"tool execution failed: {result.status}"
    return f"{result.error.code.value}: {result.error.message}"


executor = DevAssistantExecutor()
