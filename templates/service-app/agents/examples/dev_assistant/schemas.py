"""Dev assistant 的受控工具输入输出。"""

from __future__ import annotations

from typing import Literal

from agent_harness.contracts.dto import HarnessDTO


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
    result: dict[str, object] | None = None
    source_ref: str
    artifact_ref: str | None = None
    policy_decision: str | None = None
    trace_ref: str
