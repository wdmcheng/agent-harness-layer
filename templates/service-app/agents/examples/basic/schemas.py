"""Basic 模板 agent 的稳定输入输出 schema。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class Input(HarnessDTO):
    """Basic smoke 输入；允许 CLI prompt 与既有 source metadata。"""

    prompt: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Output(HarnessDTO):
    """Basic smoke 的确定性输出。"""

    result: str | None = None
    resumed: bool | None = None
