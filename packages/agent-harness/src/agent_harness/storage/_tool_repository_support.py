"""工具 repository 的 ORM 投影、租约证明与恢复原因集合。"""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

from typing import Any

from agent_harness.storage._tool_repository_contracts import (
    ToolInvocationRecord,
    ToolInvocationReplayConflict,
    WorkspaceRecord,
)
from agent_harness.storage.models import ToolInvocationModel, WorkspaceModel


def _workspace_record(model: WorkspaceModel) -> WorkspaceRecord:
    """将 workspace ORM 模型映射为公共记录，隔离 SQLAlchemy 对象生命周期。"""

    return WorkspaceRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        run_id=model.run_id,
        root_path=model.root_path,
        policy_ref=model.policy_ref,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _tool_invocation_record(model: ToolInvocationModel) -> ToolInvocationRecord:
    """将工具调用 ORM 模型映射为含 artifact 引用的领域摘要。"""

    return ToolInvocationRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        run_id=model.run_id,
        tool_name=model.tool_name,
        args_ref=model.args_ref,
        result_ref=model.result_ref,
        approval_id=model.approval_id,
        arguments_hash=model.arguments_hash,
        execution_state=model.execution_state,
        status=model.status,
        duration_ms=model.duration_ms,
        trace_id=model.trace_id,
        request_id=model.request_id,
        metadata=model.metadata_json,
        loop_id=model.loop_id,
        turn_ordinal=model.turn_ordinal,
        tool_call_id=model.tool_call_id,
        binding=model.binding_json,
        execution_lease_digest=model.execution_lease_digest,
        execution_fence=model.execution_fence,
        execution_lease_expires_at=model.execution_lease_expires_at,
        handler_started_at=model.handler_started_at,
        not_started_proof=model.not_started_proof_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _binding_digest(binding: dict[str, Any]) -> str:
    value = binding.get("binding_digest")
    if not isinstance(value, str) or len(value) != 64:
        raise ToolInvocationReplayConflict
    return value


_MODEL_TOOL_EXECUTION_REVIEW_REASONS = frozenset(
    {
        "claim_evidence_invalid",
        "commit_acknowledgement_unknown",
        "event_evidence_missing",
        "event_schema_unknown",
        "event_version_unknown",
        "executing_without_result",
        "handler_outcome_unknown",
        "result_evidence_missing",
    }
)
