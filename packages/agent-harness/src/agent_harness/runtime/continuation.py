"""Runtime checkpoint token 与 approval continuation 的确定性校验。"""

from __future__ import annotations

from typing import Any

from agent_harness.identity import IdentityContext
from agent_harness.runtime.checkpoints import IdempotencyKey, ResumeToken
from agent_harness.runtime.executor import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)
from agent_harness.runtime.state import RunStatus


class InvalidRunTransition(RuntimeError):
    """请求 terminal run 或非法状态转换时抛出。"""


def idempotency_value(key: IdempotencyKey | str | None) -> str | None:
    """将可选 IdempotencyKey DTO 或原始字符串归一为持久化查询键。"""

    return None if key is None else key.value if isinstance(key, IdempotencyKey) else key


def resume_token_value(token: ResumeToken | str) -> str:
    """将 ResumeToken DTO 或原始字符串归一为 checkpoint 存储值。"""

    return token.value if isinstance(token, ResumeToken) else token


def validate_approval_grant(
    state: dict[str, Any],
    grant: ApprovalGrant,
    authorized_tenant_id: str,
) -> None:
    """把 grant 的所有安全维度绑定到持久化 checkpoint。"""

    expected = {
        "tenant_id": state.get("tenant_id"),
        "identity_id": state.get("identity_id"),
        "agent_id": state.get("agent_id"),
        "run_id": state.get("run_id"),
        "action": state.get("action"),
        "resource": state.get("resource"),
        "arguments_hash": state.get("arguments_hash"),
    }
    actual = {
        "tenant_id": grant.tenant_id,
        "identity_id": grant.identity_id,
        "agent_id": grant.agent_id,
        "run_id": grant.run_id,
        "action": grant.action,
        "resource": grant.resource,
        "arguments_hash": grant.arguments_hash,
    }
    mismatch = next((field for field, value in expected.items() if actual[field] != value), None)
    if authorized_tenant_id != state.get("tenant_id"):
        mismatch = mismatch or "tenant_id"
    if mismatch is not None:
        raise InvalidRunTransition(f"approval grant mismatch: {mismatch}")


def checkpoint_identity(state: dict[str, Any]) -> IdentityContext:
    """恢复原执行身份，reviewer 身份只负责授权，不接管 run。"""

    payload = state.get("identity")
    if not isinstance(payload, dict):
        raise InvalidRunTransition("approval checkpoint identity is missing")
    return IdentityContext.model_validate(payload)


def optional_state_text(state: dict[str, Any], key: str) -> str | None:
    """安全读取 checkpoint 中的可选非空文本，其他类型均视为缺失。"""

    value = state.get(key)
    return value if isinstance(value, str) and value else None


def approval_checkpoint_state(
    request: AgentExecutionRequest,
    approval: AgentApprovalRequest,
    context: AgentExecutionContext,
) -> dict[str, Any]:
    """构造可持久化 continuation，不把进程内 service 放进 checkpoint。"""

    return {
        "kind": "agent_executor_approval",
        "agent_id": request.agent_id,
        "run_id": request.run_id,
        "action": approval.action,
        "resource": approval.resource,
        "arguments_ref": approval.arguments_ref,
        "arguments_hash": approval.arguments_hash,
        "continuation": approval.continuation,
        "identity_id": context.identity.user_id,
        "tenant_id": context.identity.tenant_id,
        "identity": context.identity.to_payload(),
        "request_id": context.request_id,
        "trace_id": context.trace_id,
    }


def validate_terminal_execution_result(
    run_status: RunStatus,
    result: AgentExecutionResult,
) -> None:
    """crash recovery 只能复用与已持久化 terminal 状态一致的 claim 结果。"""

    expected = "completed" if run_status == RunStatus.COMPLETED else "failed"
    if result.status != expected:
        raise InvalidRunTransition("persisted approval result does not match terminal run status")
