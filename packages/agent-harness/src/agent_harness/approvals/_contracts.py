"""Approval service 的公开结果、错误与内部纯函数。"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext
from agent_harness.runtime.checkpoints import ResumeToken
from agent_harness.runtime.executor import RunResult
from agent_harness.storage import ApprovalRecord


class ApprovalStateConflict(RuntimeError):
    """approval 已 resolved 或状态非法时抛出。"""

    def __init__(self, message: str, *, code: str = "approval.invalid_transition") -> None:
        super().__init__(message)
        self.code = code
        self.status_code = 409


class ApprovalEnqueueUnavailable(RuntimeError):
    """service approval lease 已持久化，但 queue 暂时不可用。"""

    status_code = 503
    code = "approval.enqueue_unavailable"


class ApprovalResolveResult(HarnessDTO):
    """审批 resolve 后返回审批记录和可能被推进的 run。"""

    approval: ApprovalRecord
    run: RunResult | None = None


def resume_token_value(token: ResumeToken | str | None) -> str | None:
    if token is None:
        return None
    return token.value if isinstance(token, ResumeToken) else token


def empty_args_hash() -> str:
    from agent_harness.tools import hash_tool_arguments

    return hash_tool_arguments({})


def as_utc(value: datetime) -> datetime:
    """SQLite 可能返回 naive timestamp；lease 比较统一按 UTC 处理。"""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def can_resolve_approval(actor: IdentityContext) -> bool:
    permissions = set(actor.permissions)
    roles = set(actor.roles)
    return bool(
        {"*", "approval.resolve", "approval.approve", "approval.deny"} & permissions
        or "admin" in roles
    )


def can_read_approval(actor: IdentityContext) -> bool:
    permissions = set(actor.permissions)
    roles = set(actor.roles)
    return bool({"*", "approval.read", "approval.resolve"} & permissions or "admin" in roles)
