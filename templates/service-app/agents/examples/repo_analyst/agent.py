"""只经 ToolRegistry file seam 读取 workspace 的 repo analyst。"""

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
    def __call__(
        self,
        *,
        allowed_tools: Sequence[str],
        requested_tool_name: str,
    ) -> ToolRegistry: ...


class RepoAnalystExecutor:
    """不暴露 shell，所有路径都由 composition 固定的 WorkspacePolicy 解析。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
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
        del request, context, grant
        return AgentExecutionResult.failed("repo analyst has no approval continuation")


def _input(request: AgentExecutionRequest) -> RepoAnalystInput:
    payload = dict(request.input)
    payload.pop("source", None)
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
    if data.operation == "search":
        return "file.search_files", {"query": data.query}
    if data.operation == "list":
        return "file.list_files", {"path": data.path}
    return "file.read_file", {"path": data.path}


def _summary(result: dict[str, object] | None, *, error_code: str | None) -> str:
    if error_code is not None:
        return f"workspace tool rejected the request: {error_code}"
    if result is None:
        return "workspace tool returned no inline payload; inspect artifact_ref"
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    return serialized[:500]


executor = RepoAnalystExecutor()
