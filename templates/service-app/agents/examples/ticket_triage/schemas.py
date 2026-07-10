"""Ticket triage 的结构化输入输出。"""

from __future__ import annotations

from typing import Literal

from agent_harness.contracts.dto import HarnessDTO


class TicketTriageInput(HarnessDTO):
    """待分类工单文本。"""

    text: str


class TicketTriageOutput(HarnessDTO):
    """可由 API/CLI/eval 稳定校验的分类结果。"""

    category: Literal["access", "billing", "bug", "incident", "unknown"]
    priority: Literal["low", "normal", "high", "urgent"]
    confidence: float
    route: str
    needs_review: bool
    model_provider: str
    model_trace_ref: str
