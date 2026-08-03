"""Dev assistant 的受控工具输入输出。"""

from __future__ import annotations

from typing import Literal

from agent_harness.contracts.dto import HarnessDTO


class DevAssistantToolResult(HarnessDTO):
    """只覆盖 read/write/shell 已完成载荷的严格 provider-neutral 形状。"""

    path: str | None = None
    content: str | None = None
    bytes: int | None = None
    artifact_ref: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    duration_ms: int | None = None


class DevAssistantInput(HarnessDTO):
    """只表达一次 file/shell 动作，复杂 workflow 不在本示例范围。"""

    operation: Literal["read", "write", "shell"] = "read"
    path: str = "README.md"
    content: str = ""
    command: str = "echo agent-harness"


class DevAssistantOutput(HarnessDTO):
    """工具、policy、artifact 与 trace evidence 摘要。"""

    status: str
    tool_name: str
    result: DevAssistantToolResult | None = None
    source_ref: str
    artifact_ref: str | None = None
    policy_decision: str | None = None
    trace_ref: str
