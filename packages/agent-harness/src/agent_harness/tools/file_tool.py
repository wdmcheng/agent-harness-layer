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
    """内置工作区文件工具；直接调用时也返回 ``ToolCallResult``。

    所有路径必须先经过 ``WorkspacePolicy``，危险写入和删除还必须通过已批准 grant 或
    ``PolicyEngine``；结果统一走输出守卫，避免大文件内容或敏感文本直接进入事件边界。
    """

    def __init__(
        self,
        workspace: WorkspacePolicy,
        *,
        artifact_store: FileArtifactStore,
        policy: PolicyEngine | None = None,
        inline_result_bytes: int = 8192,
    ) -> None:
        """绑定工作区、artifact 存储和可选策略入口，并固定内联结果的字节上限。"""

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
        """在工作区边界内读取 UTF-8 文件，并将内容交给输出守卫决定是否外置 artifact。"""

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
        """经审批或策略放行后写入 UTF-8 文件；路径解析仍由工作区边界强制约束。"""

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
            # 父目录只能在解析后的工作区内创建，不能由原始参数绕过根目录限制。
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
        """列出工作区内直接子项，并过滤策略标记为忽略的路径。"""

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
        """在未忽略的工作区普通文件中搜索文本，只返回匹配路径而不回传文件内容。"""

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
        """对单文件执行受控文本替换 patch。

        仅替换首个完全匹配的旧文本，找不到旧文本时不写入；这使调用方能根据结果区分
        并发或前置状态变化，而不是无界地应用模糊替换。
        """

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
        """经审批或策略放行后删除工作区内单个文件，不提供递归目录删除能力。"""

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
        """为危险文件操作复用已批准 grant，或向策略入口请求允许、拒绝或审批决定。

        返回 ``None`` 表示可以继续执行；其他返回值已经是可直接交给调用方的标准错误
        结果，调用者必须立即返回而不能在策略拒绝后继续碰触文件系统。
        """

        # grant 已由 ToolRegistry 对 approval/tenant/identity/run/action/resource/
        # arguments hash 和持久化 lease 全量校验；这里不能再次返回
        # require_approval，否则 approved continuation 永远无法执行真实文件动作。
        if context.approved_grant_id is not None:
            return None
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
    """把文件工具成功载荷包装为带独立 invocation id、artifact 引用和 trace 的标准结果。"""

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
    """把路径、策略或执行错误收敛为稳定工具错误，不泄露未授权文件内容。"""

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
    """将已验证的工作区内绝对路径转换为对调用方可见的相对 POSIX 路径。"""

    return path.relative_to(root).as_posix()


def _search_workspace(root: Path, query: str, workspace: WorkspacePolicy) -> list[dict[str, Any]]:
    """遍历未忽略工作区文件查找文本，跳过无法读取或不再满足边界的候选文件。"""

    matches: list[dict[str, Any]] = []
    if not query:
        return matches
    for path in root.rglob("*"):
        relative = _relative_to_root(path, root)
        if workspace.is_ignored(relative):
            continue
        try:
            # 每个候选再次通过 policy 解析，防止遍历期间出现的符号链接或边界变化越权。
            target = workspace.resolve(relative)
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, WorkspaceAccessError):
            continue
        if query in text:
            matches.append({"path": relative})
    return matches
