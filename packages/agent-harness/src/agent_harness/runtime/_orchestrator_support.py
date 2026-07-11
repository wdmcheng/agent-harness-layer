"""Run orchestration 各职责共享的稳定数据变换。"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_harness.identity import IdentityContext


def policy_checkpoint_state(
    *,
    run_id: str,
    agent_id: str,
    checkpoint_state: dict[str, Any],
    identity: IdentityContext,
    request_id: str | None,
    trace_id: str | None,
) -> dict[str, Any]:
    """把 input guardrail 暂停扩展为可由 APR-002 安全恢复的 checkpoint。"""

    if "policy" not in checkpoint_state or "reason" not in checkpoint_state:
        return checkpoint_state
    return {
        **checkpoint_state,
        "kind": "policy_approval",
        "agent_id": agent_id,
        "run_id": run_id,
        "action": "input.prompt_injection",
        "resource": f"agent:{agent_id}:input",
        "arguments_hash": hashlib.sha256(b"{}").hexdigest(),
        "identity_id": identity.user_id,
        "tenant_id": identity.tenant_id,
        "identity": identity.to_payload(),
        "request_id": request_id,
        "trace_id": trace_id,
    }


def run_correlation(input: dict[str, Any]) -> dict[str, Any]:
    """只把稳定、脱敏的 source/trust 引用带入公开事件。"""

    allowed = ("source", "source_ref", "trust", "trust_level")
    return {
        key: input[key] for key in allowed if isinstance(input.get(key), (str, int, float, bool))
    }
