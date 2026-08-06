"""审批后工具调用的 grant 绑定校验与稳定错误边界。"""

from __future__ import annotations

from agent_harness.runtime.executor import (
    AgentExecutionLeaseLost,
    AgentExecutionUncertain,
    ApprovalGrant,
)
from agent_harness.tools.approval_identity import hash_tool_arguments
from agent_harness.tools.types import BuiltinTool, ToolCallRequest, ToolRuntimeContext


class ApprovedToolGrantError(RuntimeError):
    """ApprovalGrant 与待执行 tool request 不匹配。"""


class ApprovedToolLeaseLost(ApprovedToolGrantError, AgentExecutionLeaseLost):
    """持久化 lease 已被新所有者接管，旧 grant 必须停止执行。"""


class ApprovedToolExecutionUncertain(AgentExecutionUncertain):
    """持久化 executing claim 没有确定性 result artifact。"""


def validate_approval_grant(
    grant: ApprovalGrant,
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    tool: BuiltinTool,
) -> None:
    """逐值校验 grant 绑定的 identity、run、tool 与参数。"""

    expected = {
        "tenant_id": context.actor.tenant_id,
        "identity_id": context.actor.user_id,
        "session_id": context.actor.session_id,
        "agent_id": request.agent_id,
        "run_id": request.run_id,
        "action": tool.action,
        "resource": tool.resource,
        "arguments_hash": hash_tool_arguments(request.arguments),
    }
    actual = {
        "tenant_id": grant.tenant_id,
        "identity_id": grant.identity_id,
        "session_id": grant.session_id,
        "agent_id": grant.agent_id,
        "run_id": grant.run_id,
        "action": grant.action,
        "resource": grant.resource,
        "arguments_hash": grant.arguments_hash,
    }
    mismatch = next((field for field, value in expected.items() if actual[field] != value), None)
    if mismatch is not None:
        raise ApprovedToolGrantError(f"approval grant mismatch: {mismatch}")


__all__ = [
    "ApprovedToolExecutionUncertain",
    "ApprovedToolGrantError",
    "ApprovedToolLeaseLost",
    "validate_approval_grant",
]
