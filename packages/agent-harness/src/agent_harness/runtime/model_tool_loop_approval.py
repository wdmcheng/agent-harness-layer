"""模型工具循环审批快照的 durable artifact 与 active lease 解析。"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ConfigDict, Field

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.tool_catalog import provider_tool_catalog_bytes
from agent_harness.runtime.executor import AgentApprovalRequest, ApprovalGrant
from agent_harness.runtime.model_tool_loop import (
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopError,
)
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.tools.approval_identity import hash_tool_arguments


class _ModelToolLoopContinuation(HarnessDTO):
    """checkpoint 和 approval metadata 共用的最小 exact 续跑身份。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["model-tool-loop-continuation-v1"] = "model-tool-loop-continuation-v1"
    kind: Literal["model_tool_loop"] = "model_tool_loop"
    snapshot_ref: str = Field(pattern=r"^artifact://[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    loop_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_ordinal: int = Field(gt=0, strict=True)
    tool_call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(min_length=1)


class _ModelToolLoopApprovalArtifact(HarnessDTO):
    """artifact 外层版本标记；snapshot DTO 自身负责全部字段摘要重验。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["model-tool-loop-approval-artifact-v1"] = (
        "model-tool-loop-approval-artifact-v1"
    )
    snapshot: ModelToolLoopApprovalSnapshot


def _catalog_payload(snapshot: ModelToolLoopApprovalSnapshot) -> dict[str, Any]:
    """显式保留 ToolCatalog 公共投影刻意排除的严格 input schema body。"""

    return {
        "schema_version": snapshot.catalog.schema_version,
        "catalog_digest": snapshot.catalog.catalog_digest,
        "tools": [
            {
                **item.model_dump(mode="json"),
                "input_schema": item.input_schema.model_dump(mode="json"),
            }
            for item in snapshot.catalog.tools
        ],
    }


def _artifact_payload(snapshot: ModelToolLoopApprovalSnapshot) -> dict[str, Any]:
    """构造可跨进程重载的完整 snapshot payload，不序列化运行时对象。"""

    snapshot_payload = snapshot.model_dump(mode="json")
    snapshot_payload["catalog"] = _catalog_payload(snapshot)
    return {
        "schema_version": "model-tool-loop-approval-artifact-v1",
        "snapshot": snapshot_payload,
    }


def _continuation(
    snapshot: ModelToolLoopApprovalSnapshot,
    *,
    snapshot_ref: str,
) -> _ModelToolLoopContinuation:
    """从同一 frozen snapshot 派生公开 checkpoint 所需的去敏身份。"""

    intent = snapshot.intent
    return _ModelToolLoopContinuation(
        snapshot_ref=snapshot_ref,
        snapshot_digest=snapshot.snapshot_digest,
        loop_id=intent.loop_id,
        turn_ordinal=intent.turn_ordinal,
        tool_call_id=intent.tool_call_id,
        catalog_digest=intent.catalog_digest,
        arguments_digest=intent.arguments_digest,
        session_id=snapshot.session_id,
    )


class ModelToolLoopApprovalStore:
    """复用 ApprovalService lease，以 artifact 保存 exact 模型工具循环位置。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        artifact_store: FileArtifactStore,
        trusted_clock: Callable[[], datetime] | None = None,
        max_grant_age_seconds: float = 300.0,
    ) -> None:
        """保存UoW、artifact和与ApprovalService一致的grant新鲜度边界。"""

        if (
            isinstance(max_grant_age_seconds, bool)
            or not math.isfinite(float(max_grant_age_seconds))
            or max_grant_age_seconds < 0
        ):
            raise ValueError("model tool approval grant age must be finite and non-negative")

        self._storage = storage
        self._artifact_store = artifact_store
        self._trusted_clock = trusted_clock or (lambda: datetime.now(UTC))
        self._max_grant_age = timedelta(seconds=float(max_grant_age_seconds))

    def create(
        self,
        *,
        snapshot: ModelToolLoopApprovalSnapshot,
        reason: str,
    ) -> AgentApprovalRequest:
        """先 materialize 完整快照，再返回供 orchestrator 建 checkpoint 的引用。"""

        if type(snapshot) is not ModelToolLoopApprovalSnapshot:
            raise ModelToolLoopError("tool.approval_invalid")
        artifact = self._artifact_store.write_json(_artifact_payload(snapshot))
        continuation = _continuation(snapshot, snapshot_ref=artifact.ref)
        return AgentApprovalRequest(
            action=snapshot.action,
            resource=snapshot.resource,
            reason=reason,
            arguments_ref=artifact.ref,
            arguments_hash=hash_tool_arguments(snapshot.intent.arguments),
            continuation=continuation.model_dump(mode="json"),
        )

    async def resolve(
        self,
        *,
        grant: ApprovalGrant,
    ) -> ModelToolLoopApprovalSnapshot:
        """在任何 approved handler 前重验 durable lease、metadata 与 artifact。"""

        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(grant.approval_id)
        if lease is None:
            raise ModelToolLoopError("tool.approval_invalid")
        if lease.lease_id != grant.lease_id:
            raise ModelToolLoopError("approval.resolution_in_progress")
        if lease.approval.status == "denied":
            raise ModelToolLoopError("approval.denied")
        if lease.approval.status != "waiting":
            raise ModelToolLoopError("approval.invalid_transition")
        if lease.state == "revoked":
            raise ModelToolLoopError("approval.revoked")
        if lease.state == "needs_review":
            raise ModelToolLoopError("approval.execution_needs_review")
        if lease.state not in {"claimed", "execution_owned", "recovery_pending"}:
            raise ModelToolLoopError("approval.resolution_in_progress")
        if lease.claimed_at is None:
            raise ModelToolLoopError("tool.approval_invalid")
        claimed_at = _as_utc(lease.claimed_at)
        now = _as_utc(self._trusted_clock())
        if now >= claimed_at + self._max_grant_age:
            raise ModelToolLoopError("approval.expired")

        approval = lease.approval
        expected_grant = {
            "tenant_id": approval.tenant_id,
            "identity_id": str(approval.metadata.get("identity_id") or ""),
            "session_id": str(approval.metadata.get("session_id") or ""),
            "agent_id": approval.agent_id,
            "run_id": approval.run_id,
            "action": approval.action,
            "resource": approval.resource,
            "arguments_hash": str(approval.metadata.get("arguments_hash") or ""),
        }
        actual_grant = {
            "tenant_id": grant.tenant_id,
            "identity_id": grant.identity_id,
            "session_id": grant.session_id,
            "agent_id": grant.agent_id,
            "run_id": grant.run_id,
            "action": grant.action,
            "resource": grant.resource,
            "arguments_hash": grant.arguments_hash,
        }
        if expected_grant != actual_grant:
            raise ModelToolLoopError("tool.approval_invalid")

        try:
            continuation = _ModelToolLoopContinuation.model_validate(
                approval.metadata.get("continuation")
            )
            arguments_ref = approval.metadata.get("arguments_ref")
            if arguments_ref != continuation.snapshot_ref:
                raise ValueError("approval snapshot ref mismatch")
            raw = self._artifact_store.read_json(continuation.snapshot_ref)
            if self._artifact_store.reference_json(raw).ref != continuation.snapshot_ref:
                raise ValueError("approval artifact checksum mismatch")
            artifact = _ModelToolLoopApprovalArtifact.model_validate(raw)
            snapshot = artifact.snapshot.model_copy(deep=True)
            provider_tool_catalog_bytes(snapshot.catalog)
        except ModelToolLoopError:
            raise
        except Exception:
            raise ModelToolLoopError("tool.approval_invalid") from None

        persisted = _continuation(snapshot, snapshot_ref=continuation.snapshot_ref)
        if (
            persisted != continuation
            or continuation.snapshot_digest != snapshot.snapshot_digest
            or approval.action != snapshot.action
            or approval.resource != snapshot.resource
            or grant.arguments_hash != hash_tool_arguments(snapshot.intent.arguments)
            or grant.identity_id != snapshot.identity_id
            or grant.session_id != snapshot.session_id
        ):
            raise ModelToolLoopError("tool.approval_invalid")
        return snapshot


def _as_utc(value: datetime) -> datetime:
    """统一SQLite naive时间与受信时钟，避免跨数据库新鲜度判断漂移。"""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["ModelToolLoopApprovalStore"]
