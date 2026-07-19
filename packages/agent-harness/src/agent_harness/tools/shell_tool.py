"""受 workspace、allowlist 和 timeout 约束的 Shell 工具。"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from fnmatch import fnmatch
from pathlib import Path
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.tools.output_guard import write_stream_artifact
from agent_harness.tools.types import (
    ToolCallRequest,
    ToolCallResult,
    ToolError,
    ToolErrorCode,
    ToolRuntimeContext,
    tool_status_for_error,
)
from agent_harness.tools.workspace import WorkspaceAccessError, WorkspacePolicy


class ShellTool:
    """本地命令执行工具；默认禁用，必须显式配置才执行。"""

    def __init__(
        self,
        *,
        workspace: WorkspacePolicy,
        artifact_store: FileArtifactStore,
        enabled: bool = False,
        allowlist: list[str] | None = None,
        denylist: list[str] | None = None,
        env_whitelist: list[str] | None = None,
        timeout_seconds: int = 30,
        inline_output_bytes: int = 8192,
    ) -> None:
        """保存 workspace、命令策略、环境白名单和输出截断边界。

        默认 ``enabled=False`` 且空 allowlist 也会拒绝执行，调用方必须显式打开两层
        开关，避免配置遗漏时获得宿主命令能力。
        """

        self._workspace = workspace
        self._artifact_store = artifact_store
        self._enabled = enabled
        self._allowlist = allowlist or []
        self._denylist = denylist or []
        self._env_whitelist = env_whitelist or []
        self._timeout_seconds = timeout_seconds
        self._inline_output_bytes = inline_output_bytes

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        """在预检通过后于受限 workspace 执行命令，并将大输出外置为 artifact。

        子进程从白名单环境构造，stdout/stderr 分别截断和留存引用；这样返回载荷可控，
        审计仍能追溯完整输出。
        """

        invocation_id = str(uuid4())
        source_ref = f"tool://{request.tool_name}/{context.run_id or 'adhoc'}/{invocation_id}"
        command = str(request.arguments.get("command", ""))
        # 所有拒绝都在创建子进程前返回，避免 allowlist/workspace 绕过产生副作用。
        preflight_error = self.preflight(request, context=context)
        if preflight_error is not None:
            return preflight_error
        argv = shlex.split(command)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=self._workspace.root,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                env=_filtered_env(self._env_whitelist),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return _shell_error(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.TIMEOUT,
                f"command timed out after {exc.timeout} seconds",
                result={
                    "exit_code": None,
                    "duration_ms": duration_ms,
                    "timed_out": True,
                    "stdout_ref": None,
                    "stderr_ref": None,
                },
                truncation={"truncated": False, "stdout": {}, "stderr": {}},
            )
        except OSError as exc:
            return _shell_error(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.EXECUTION_FAILED,
                str(exc),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        # 两条流独立落 artifact，避免 stderr 大量输出挤掉 stdout 的可诊断内容。
        stdout, stdout_ref, stdout_truncation = write_stream_artifact(
            artifact_store=self._artifact_store,
            tool_name=request.tool_name,
            invocation_id=invocation_id,
            stream=completed.stdout,
            stream_name="stdout",
            inline_bytes=self._inline_output_bytes,
        )
        stderr, stderr_ref, stderr_truncation = write_stream_artifact(
            artifact_store=self._artifact_store,
            tool_name=request.tool_name,
            invocation_id=invocation_id,
            stream=completed.stderr,
            stream_name="stderr",
            inline_bytes=self._inline_output_bytes,
        )
        result = {
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_ref": stdout_ref,
            "stderr_ref": stderr_ref,
            "duration_ms": duration_ms,
        }
        stdout_truncated = bool(stdout_truncation.get("truncated"))
        stderr_truncated = bool(stderr_truncation.get("truncated"))
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed" if completed.returncode == 0 else "failed",
            invocation_id=invocation_id,
            result=result,
            source_ref=source_ref,
            artifact_ref=stdout_ref or stderr_ref,
            truncation={
                "truncated": stdout_truncated or stderr_truncated,
                "stdout": stdout_truncation,
                "stderr": stderr_truncation,
            },
            request_id=context.request_id or request.request_id,
            trace_id=context.trace_id or request.trace_id,
        )

    def preflight(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult | None:
        """验证启用状态、Shell 解析、allowlist/denylist 和 workspace 路径边界。

        返回已格式化的工具错误而非抛异常，使调用方可以把拒绝与执行结果使用同一审计
        和 API 载荷处理。
        """

        invocation_id = str(uuid4())
        source_ref = f"tool://{request.tool_name}/{context.run_id or 'adhoc'}/{invocation_id}"
        command = str(request.arguments.get("command", ""))
        if not self._enabled:
            return _shell_error(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.DISABLED,
                "shell tool is disabled",
            )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return _shell_error(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.EXECUTION_FAILED,
                str(exc),
            )
        if not _allowed(command, argv, self._allowlist, self._denylist):
            return _shell_error(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.ALLOWLIST_DENIED,
                "command is not allowed by shell allowlist",
            )
        workspace_error = _workspace_argument_error(argv, self._workspace)
        if workspace_error is not None:
            return _shell_error(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.WORKSPACE_DENIED,
                str(workspace_error),
            )
        return None


def _allowed(
    command: str,
    argv: list[str],
    allowlist: list[str],
    denylist: list[str],
) -> bool:
    """按可执行文件和完整命令匹配策略；denylist 优先且空 allowlist 默认拒绝。"""

    executable = argv[0] if argv else ""
    if not executable:
        return False
    if any(fnmatch(executable, pattern) or fnmatch(command, pattern) for pattern in denylist):
        return False
    if not allowlist:
        return False
    return any(fnmatch(executable, pattern) or fnmatch(command, pattern) for pattern in allowlist)


def _workspace_argument_error(
    argv: list[str],
    workspace: WorkspacePolicy,
) -> WorkspaceAccessError | None:
    """检查命令参数中可能代表路径的值，拒绝逃离 workspace 的引用。"""

    for index, argument in enumerate(argv):
        # 可执行文件本身允许绝对路径；其余候选路径必须接受 workspace 策略校验。
        for candidate in _path_candidates(argument):
            if index == 0 and Path(candidate).is_absolute():
                continue
            if not _should_check_path_argument(candidate, workspace.root):
                continue
            try:
                workspace.resolve(candidate)
            except WorkspaceAccessError as exc:
                return exc
    return None


def _path_candidates(argument: str) -> list[str]:
    """同时返回原参数和 ``key=value`` 形式中的值部分，覆盖常见工具参数写法。"""

    candidates = [argument]
    if "=" in argument:
        candidates.append(argument.split("=", 1)[1])
    return candidates


def _should_check_path_argument(argument: str, root: Path) -> bool:
    """识别可能解析为文件路径的参数，避免把普通选项和文字误判为路径。"""

    if not argument or argument in {"-", "--"}:
        return False
    if argument.startswith("-") and "/" not in argument and "=" not in argument:
        return False
    if argument.startswith(("~", "/", "../")) or argument == "..":
        return True
    if "/../" in argument or argument.endswith("/.."):
        return True
    if "/" in argument:
        return True
    candidate = root / argument
    return candidate.exists() or candidate.is_symlink()


def _filtered_env(env_whitelist: list[str]) -> dict[str, str]:
    """从当前进程仅复制白名单环境变量，不继承密钥或代理等隐式状态。"""

    return {key: os.environ[key] for key in env_whitelist if key in os.environ}


def _shell_error(
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    invocation_id: str,
    source_ref: str,
    code: ToolErrorCode,
    message: str,
    *,
    result: dict[str, object] | None = None,
    truncation: dict[str, object] | None = None,
) -> ToolCallResult:
    """构造带稳定 invocation/source 身份的工具错误结果，供审计与 API 共用。"""

    return ToolCallResult(
        tool_name=request.tool_name,
        status=tool_status_for_error(code),
        invocation_id=invocation_id,
        result=result,
        error=ToolError(code=code, message=message),
        source_ref=source_ref,
        truncation=truncation or {"truncated": False},
        request_id=context.request_id or request.request_id,
        trace_id=context.trace_id or request.trace_id,
    )
