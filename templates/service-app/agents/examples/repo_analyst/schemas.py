"""Repo analyst 的 workspace-safe 输入输出。"""

from __future__ import annotations

from typing import Literal

from agent_harness.contracts.dto import HarnessDTO


class RepoAnalystInput(HarnessDTO):
    """只允许 file read/search/list 的分析请求。"""

    operation: Literal["read", "search", "list"] = "read"
    path: str = "README.md"
    query: str = ""


class RepoAnalystOutput(HarnessDTO):
    """保留 tool source/artifact evidence 的分析结果。"""

    status: str
    operation: str
    summary: str
    source_ref: str
    artifact_ref: str | None = None
    error_code: str | None = None
    trust_level: str = "untrusted"
    trace_ref: str
