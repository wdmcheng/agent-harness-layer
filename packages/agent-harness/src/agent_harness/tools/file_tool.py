"""受 workspace policy 约束的文件工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.tools.output_guard import guarded_tool_payload
from agent_harness.tools.types import (
    ToolCallRequest,
    ToolCallResult,
    ToolError,
    ToolErrorCode,
    ToolRuntimeContext,
    tool_status_for_error,
)
from agent_harness.tools.workspace import WorkspaceAccessError, WorkspacePolicy


class FileTool:
    """内置 workspace 文件工具；直接调用时也返回 ToolCallResult。"""

    def __init__(
        self,
        workspace: WorkspacePolicy,
        *,
        artifact_store: FileArtifactStore,
        policy: PolicyEngine | None = None,
        inline_result_bytes: int = 8192,
    ) -> None:
        self._workspace = workspace
        self._artifact_store = artifact_store
        self._policy = policy
        self._inline_result_bytes = inline_result_bytes

    async def read_file(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        path = str(request.arguments.get("path", ""))
        try:
            target = self._workspace.resolve(path)
            payload = {
                "path": _relative_to_root(target, self._workspace.root),
                "content": target.read_text(encoding="utf-8"),
            }
        except (OSError, WorkspaceAccessError, UnicodeDecodeError) as exc:
            return _file_error(request, context, ToolErrorCode.WORKSPACE_DENIED, str(exc))
        return _file_success(
            request,
            context,
            payload,
            artifact_store=self._artifact_store,
            inline_result_bytes=self._inline_result_bytes,
        )

    async def write_file(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        policy_error = await self._dangerous_policy_error(
            request,
            context,
            action="file.bulk_write",
            resource="workspace:file",
        )
        if policy_error is not None:
            return policy_error
        path = str(request.arguments.get("path", ""))
        content = str(request.arguments.get("content", ""))
        try:
            target = self._workspace.resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            payload = {
                "path": _relative_to_root(target, self._workspace.root),
                "bytes": len(content.encode()),
            }
        except (OSError, WorkspaceAccessError) as exc:
            return _file_error(request, context, ToolErrorCode.WORKSPACE_DENIED, str(exc))
        return _file_success(
            request,
            context,
            payload,
            artifact_store=self._artifact_store,
            inline_result_bytes=self._inline_result_bytes,
        )

    async def list_files(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        path = str(request.arguments.get("path", "."))
        try:
            target = self._workspace.resolve(path)
            entries = [
                _relative_to_root(item, self._workspace.root)
                for item in sorted(target.iterdir())
                if not self._workspace.is_ignored(_relative_to_root(item, self._workspace.root))
            ]
        except (OSError, WorkspaceAccessError) as exc:
            return _file_error(request, context, ToolErrorCode.WORKSPACE_DENIED, str(exc))
        return _file_success(
            request,
            context,
            {"path": path, "entries": entries},
            artifact_store=self._artifact_store,
            inline_result_bytes=self._inline_result_bytes,
        )

    async def search_files(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        query = str(request.arguments.get("query", ""))
        try:
            matches = _search_workspace(self._workspace.root, query, self._workspace)
        except OSError as exc:
            return _file_error(request, context, ToolErrorCode.WORKSPACE_DENIED, str(exc))
        return _file_success(
            request,
            context,
            {"query": query, "matches": matches},
            artifact_store=self._artifact_store,
            inline_result_bytes=self._inline_result_bytes,
        )

    async def apply_patch(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        """对单文件执行受控文本替换 patch。"""

        policy_error = await self._dangerous_policy_error(
            request,
            context,
            action="file.bulk_write",
            resource="workspace:file",
        )
        if policy_error is not None:
            return policy_error
        path = str(request.arguments.get("path", ""))
        old = str(request.arguments.get("old", ""))
        new = str(request.arguments.get("new", ""))
        try:
            target = self._workspace.resolve(path)
            original = target.read_text(encoding="utf-8")
            if old not in original:
                return _file_error(
                    request,
                    context,
                    ToolErrorCode.EXECUTION_FAILED,
                    "patch old text not found",
                )
            updated = original.replace(old, new, 1)
            target.write_text(updated, encoding="utf-8")
            payload = {
                "path": _relative_to_root(target, self._workspace.root),
                "replacements": 1,
                "bytes_delta": len(updated.encode()) - len(original.encode()),
            }
        except (OSError, WorkspaceAccessError, UnicodeDecodeError) as exc:
            return _file_error(request, context, ToolErrorCode.WORKSPACE_DENIED, str(exc))
        return _file_success(
            request,
            context,
            payload,
            artifact_store=self._artifact_store,
            inline_result_bytes=self._inline_result_bytes,
        )

    async def delete_file(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        policy_error = await self._dangerous_policy_error(
            request,
            context,
            action="file.delete",
            resource="workspace:file",
        )
        if policy_error is not None:
            return policy_error
        path = str(request.arguments.get("path", ""))
        try:
            target = self._workspace.resolve(path)
            target.unlink()
            payload = {"path": _relative_to_root(target, self._workspace.root), "deleted": True}
        except (OSError, WorkspaceAccessError) as exc:
            return _file_error(request, context, ToolErrorCode.WORKSPACE_DENIED, str(exc))
        return _file_success(
            request,
            context,
            payload,
            artifact_store=self._artifact_store,
            inline_result_bytes=self._inline_result_bytes,
        )

    async def _dangerous_policy_error(
        self,
        request: ToolCallRequest,
        context: ToolRuntimeContext,
        *,
        action: str,
        resource: str,
    ) -> ToolCallResult | None:
        if self._policy is None:
            return _file_error(
                request,
                context,
                ToolErrorCode.POLICY_DENIED,
                "dangerous file operation requires PolicyEngine",
            )
        decision = await self._policy.evaluate(
            PolicyCheck(
                actor=context.actor,
                action=action,
                resource=resource,
                context={
                    "tool_name": request.tool_name,
                    "agent_id": context.agent_id,
                    "run_id": context.run_id,
                    "tenant_id": context.actor.tenant_id,
                    "user_id": context.actor.user_id,
                    "request_id": context.request_id or request.request_id,
                    "trace_id": context.trace_id or request.trace_id,
                },
            )
        )
        if decision.decision == GuardrailDecisionStatus.ALLOW.value:
            return None
        code = (
            ToolErrorCode.APPROVAL_REQUIRED
            if decision.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value
            else ToolErrorCode.POLICY_DENIED
        )
        return _file_error(request, context, code, decision.reason, policy=decision.to_payload())


def _file_success(
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    payload: dict[str, Any],
    *,
    artifact_store: FileArtifactStore,
    inline_result_bytes: int,
) -> ToolCallResult:
    invocation_id = str(uuid4())
    result, artifact_ref, truncation = guarded_tool_payload(
        tool_name=request.tool_name,
        invocation_id=invocation_id,
        payload=payload,
        artifact_store=artifact_store,
        inline_bytes=inline_result_bytes,
    )
    return ToolCallResult(
        tool_name=request.tool_name,
        status="completed",
        invocation_id=invocation_id,
        result=result,
        source_ref=f"tool://{request.tool_name}/{context.run_id or 'adhoc'}/{invocation_id}",
        artifact_ref=artifact_ref,
        truncation=truncation,
        request_id=context.request_id or request.request_id,
        trace_id=context.trace_id or request.trace_id,
    )


def _file_error(
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    code: ToolErrorCode,
    message: str,
    *,
    policy: dict[str, Any] | None = None,
) -> ToolCallResult:
    invocation_id = str(uuid4())
    return ToolCallResult(
        tool_name=request.tool_name,
        status=tool_status_for_error(code),
        invocation_id=invocation_id,
        error=ToolError(code=code, message=message),
        source_ref=f"tool://{request.tool_name}/{context.run_id or 'adhoc'}/{invocation_id}",
        policy=policy or {},
        request_id=context.request_id or request.request_id,
        trace_id=context.trace_id or request.trace_id,
    )


def _relative_to_root(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _search_workspace(root: Path, query: str, workspace: WorkspacePolicy) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if not query:
        return matches
    for path in root.rglob("*"):
        relative = _relative_to_root(path, root)
        if workspace.is_ignored(relative):
            continue
        try:
            target = workspace.resolve(relative)
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, WorkspaceAccessError):
            continue
        if query in text:
            matches.append({"path": relative})
    return matches
