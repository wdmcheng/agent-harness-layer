"""HITL approval 状态机公开 seam。"""

from agent_harness.approvals.service import (
    ApprovalEnqueueUnavailable as ApprovalEnqueueUnavailable,
)
from agent_harness.approvals.service import ApprovalResolveResult as ApprovalResolveResult
from agent_harness.approvals.service import ApprovalService as ApprovalService
from agent_harness.approvals.service import ApprovalStateConflict as ApprovalStateConflict

_APPROVAL_EXPORTS = [
    "ApprovalResolveResult",
    "ApprovalEnqueueUnavailable",
    "ApprovalService",
    "ApprovalStateConflict",
]

__all__ = [*_APPROVAL_EXPORTS]  # pyright: ignore[reportUnsupportedDunderAll]
