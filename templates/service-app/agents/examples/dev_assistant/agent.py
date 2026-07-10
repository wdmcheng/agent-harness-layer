"""通过 ToolRegistry、PolicyEngine 和 ApprovalGrant 执行 file/shell 动作。"""

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
    def __call__(
        self,
        *,
        allowed_tools: Sequence[str],
        requested_tool_name: str,
    ) -> ToolRegistry: ...


class DevAssistantExecutor:
    """普通调用只做 policy preflight；approved continuation 才进入 handler。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
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
    if result.error is None:
        return f"tool execution failed: {result.status}"
    return f"{result.error.code.value}: {result.error.message}"


executor = DevAssistantExecutor()
