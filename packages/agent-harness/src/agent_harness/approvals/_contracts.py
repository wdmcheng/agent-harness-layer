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
        """保留稳定错误码与 HTTP 409 语义，供 API/CLI 使用同一冲突映射。"""

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
    """将 runtime DTO 或已序列化 token 归一为存储/API 可用的字符串值。"""

    if token is None:
        return None
    return token.value if isinstance(token, ResumeToken) else token


def empty_args_hash() -> str:
    """生成空工具参数的规范哈希，避免不同调用点各自定义默认值。"""

    from agent_harness.tools import hash_tool_arguments

    return hash_tool_arguments({})


def as_utc(value: datetime) -> datetime:
    """SQLite 可能返回 naive timestamp；lease 比较统一按 UTC 处理。"""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def can_resolve_approval(actor: IdentityContext) -> bool:
    """判断主体是否拥有审批结论权限；管理员与通配权限保持显式兼容。"""

    permissions = set(actor.permissions)
    roles = set(actor.roles)
    return bool(
        {"*", "approval.resolve", "approval.approve", "approval.deny"} & permissions
        or "admin" in roles
    )


def can_read_approval(actor: IdentityContext) -> bool:
    """判断主体是否可读取审批；resolve 权限隐含读取以支持处理工作流。"""

    permissions = set(actor.permissions)
    roles = set(actor.roles)
    return bool({"*", "approval.read", "approval.resolve"} & permissions or "admin" in roles)
