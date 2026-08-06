"""工具 repository 的公开 DTO、执行 claim 与稳定冲突信号。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from agent_harness.contracts.dto import HarnessDTO


class WorkspaceCreate(HarnessDTO):
    """创建 workspace 记录的公开输入。"""

    tenant_id: str
    agent_id: str
    run_id: str | None = None
    root_path: str
    policy_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceRecord(WorkspaceCreate):
    """已持久化 workspace 摘要。"""

    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ToolInvocationCreate(HarnessDTO):
    """创建 tool_invocations 记录的公开输入。"""

    tenant_id: str
    agent_id: str
    run_id: str | None = None
    tool_name: str
    args_ref: str
    result_ref: str | None = None
    approval_id: str | None = None
    arguments_hash: str | None = None
    execution_state: str | None = None
    status: str
    duration_ms: int | None = None
    trace_id: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    loop_id: str | None = None
    turn_ordinal: int | None = None
    tool_call_id: str | None = None
    binding: dict[str, Any] | None = None
    execution_lease_digest: str | None = None
    execution_fence: int | None = None
    execution_lease_expires_at: datetime | None = None
    handler_started_at: datetime | None = None
    not_started_proof: dict[str, Any] | None = None


class ToolInvocationRecord(ToolInvocationCreate):
    """已持久化工具调用摘要。"""

    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ModelToolInvocationClaimCreate(HarnessDTO):
    """新模型驱动工具调用在handler前冻结的唯一claim输入。"""

    tenant_id: str
    agent_id: str
    run_id: str
    tool_name: str
    args_ref: str
    approval_id: str | None = None
    arguments_hash: str
    trace_id: str
    request_id: str | None = None
    loop_id: str
    turn_ordinal: int
    tool_call_id: str
    binding: dict[str, Any]
    execution_lease_digest: str
    execution_fence: int
    execution_lease_expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolHandlerNotStartedProof(HarnessDTO):
    """过期claimed换租时可公开重算的handler未开始证据。"""

    schema_version: Literal["tool-handler-not-started-v1"]
    tool_call_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    binding_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    prior_fence: int = Field(ge=1, strict=True)
    next_fence: int = Field(ge=2, strict=True)
    previous_lease_expires_at: str
    reason: Literal["claim_lease_expired"]
    proof_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        tool_call_id: str,
        binding_digest: str,
        prior_fence: int,
        next_fence: int,
        previous_lease_expires_at: datetime,
    ) -> Self:
        """从旧owner事实构造唯一canonical preimage并生成摘要。"""

        expiry = _as_utc_required(previous_lease_expires_at).isoformat()
        preimage = _tool_handler_not_started_preimage(
            tool_call_id=tool_call_id,
            binding_digest=binding_digest,
            prior_fence=prior_fence,
            next_fence=next_fence,
            previous_lease_expires_at=expiry,
        )
        return cls(
            **preimage,
            proof_digest=_canonical_proof_digest(preimage),
        )

    @model_validator(mode="after")
    def validate_canonical_proof(self) -> Self:
        """拒绝非连续fence、非canonical UTC时间和不可复算摘要。"""

        if self.next_fence != self.prior_fence + 1:
            raise ValueError("tool handler proof fence must advance exactly once")
        try:
            expiry = datetime.fromisoformat(self.previous_lease_expires_at)
        except ValueError as exc:
            raise ValueError("tool handler proof lease expiry must be ISO-8601") from exc
        if (
            expiry.tzinfo is None
            or expiry.utcoffset() is None
            or expiry.astimezone(UTC).isoformat() != self.previous_lease_expires_at
        ):
            raise ValueError("tool handler proof lease expiry must be canonical UTC")
        preimage = _tool_handler_not_started_preimage(
            tool_call_id=self.tool_call_id,
            binding_digest=self.binding_digest,
            prior_fence=self.prior_fence,
            next_fence=self.next_fence,
            previous_lease_expires_at=self.previous_lease_expires_at,
        )
        if self.proof_digest != _canonical_proof_digest(preimage):
            raise ValueError("tool handler proof digest mismatch")
        return self


class ToolInvocationReplayConflict(RuntimeError):
    """相同tool/approval identity携带不同绑定时的稳定失败。"""

    code = "tool.execution_replay_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


def _as_utc_required(value: datetime | None) -> datetime:
    """统一 SQLite 与 PostgreSQL 时间形状，并拒绝缺失租约期限。"""

    if value is None:
        raise ToolInvocationReplayConflict
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _tool_handler_not_started_preimage(
    *,
    tool_call_id: str,
    binding_digest: str,
    prior_fence: int,
    next_fence: int,
    previous_lease_expires_at: str,
) -> dict[str, Any]:
    """构造 handler 未启动证明的唯一 canonical preimage。"""

    return {
        "schema_version": "tool-handler-not-started-v1",
        "tool_call_id": tool_call_id,
        "binding_digest": binding_digest,
        "prior_fence": prior_fence,
        "next_fence": next_fence,
        "previous_lease_expires_at": previous_lease_expires_at,
        "reason": "claim_lease_expired",
    }


def _canonical_proof_digest(preimage: dict[str, Any]) -> str:
    """对证明或 review preimage 计算稳定 SHA-256 摘要。"""

    canonical = json.dumps(
        preimage,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = [
    "WorkspaceCreate",
    "WorkspaceRecord",
    "ToolInvocationCreate",
    "ToolInvocationRecord",
    "ModelToolInvocationClaimCreate",
    "ToolHandlerNotStartedProof",
    "ToolInvocationReplayConflict",
]
