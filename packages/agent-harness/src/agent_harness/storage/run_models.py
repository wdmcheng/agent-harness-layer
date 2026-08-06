"""租户、会话、run、checkpoint 与上下文缓存 ORM。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from agent_harness.storage.orm_base import Base as Base
from agent_harness.storage.orm_base import TimestampMixin as TimestampMixin


class TenantModel(TimestampMixin, Base):
    """轻量租户上下文，单租户 local profile 使用 default。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)


class SessionModel(TimestampMixin, Base):
    """一次用户/agent 会话归属，供 run、trace 和审计串联。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AgentRunModel(TimestampMixin, Base):
    """run 生命周期主记录，保存幂等键、输入、输出和状态。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "agent_id",
            "idempotency_key",
            name="uq_agent_runs_idempotency",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_agent_runs_id_tenant"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "trace_id",
            name="uq_agent_runs_id_tenant_trace",
        ),
        ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_agent_runs_parent_tenant",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["trace_id", "tenant_id"],
            ["run_trace_bindings.trace_id", "run_trace_bindings.tenant_id"],
            name="fk_agent_runs_trace_binding_tenant",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created", index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    queue_operation_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    queue_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    queue_effective_idempotency_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    queue_enqueue_state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    queue_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_owner_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    execution_workflow_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunTraceBindingModel(Base):
    """root lineage 的全局 trace claim 与直接 tenant 归属。"""

    __tablename__ = "run_trace_bindings"
    __table_args__ = (
        UniqueConstraint("root_run_id", name="uq_run_trace_bindings_root_run_id"),
        UniqueConstraint(
            "trace_id",
            "tenant_id",
            name="uq_run_trace_bindings_trace_tenant",
        ),
        ForeignKeyConstraint(
            ["root_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_trace_bindings_root_tenant",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    trace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    root_run_id: Mapped[str] = mapped_column(String(36), nullable=False)


class CheckpointModel(Base):
    """checkpoint/resume token 的持久化真相源。"""

    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_checkpoints_run_sequence"),
        UniqueConstraint("resume_token", name="uq_checkpoints_resume_token"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_token: Mapped[str] = mapped_column(String(255), nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ContextAssemblyModel(TimestampMixin, Base):
    """ContextAssembler 的持久化 trace 摘要。"""

    __tablename__ = "context_assemblies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "loop_id",
            "turn_ordinal",
            name="uq_context_assemblies_tenant_loop_turn",
        ),
        CheckConstraint(
            "(loop_id is null and turn_ordinal is null and tool_call_id is null "
            "and input_identity_digest is null and output_digest is null) or ("
            "loop_id is not null and turn_ordinal is not null and tool_call_id is not null "
            "and input_identity_digest is not null and output_digest is not null "
            "and length(loop_id) = 64 and turn_ordinal >= 1 and length(tool_call_id) = 64 "
            "and length(input_identity_digest) = 64 and length(output_digest) = 64 "
            "and run_id is not null)",
            name="ck_context_assemblies_model_loop_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id"),
        nullable=True,
    )
    input_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    truncation_summary_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    output_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    loop_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    turn_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_identity_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ModelToolLoopSchemaMarkerModel(TimestampMixin, Base):
    """记录0018 v1 evidence是否曾出现；业务repository只能单调写true。"""

    __tablename__ = "model_tool_loop_schema_marker"
    __table_args__ = (
        CheckConstraint(
            "marker_key = 'model-tool-loop-v1'",
            name="ck_model_tool_loop_schema_marker_key",
        ),
    )

    marker_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    evidence_seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


def _validate_model_tool_loop_marker_update(
    _mapper: Mapper[ModelToolLoopSchemaMarkerModel],
    _connection: Connection,
    target: ModelToolLoopSchemaMarkerModel,
) -> None:
    """ORM维护入口只能保持或提升marker，不能把true清回false。"""

    history = inspect(target).attrs.evidence_seen.history
    if any(value is True for value in history.deleted) and any(
        value is False for value in history.added
    ):
        raise ValueError("model tool loop schema marker is monotonic")


def _reject_model_tool_loop_marker_delete(
    _mapper: Mapper[ModelToolLoopSchemaMarkerModel],
    _connection: Connection,
    _target: ModelToolLoopSchemaMarkerModel,
) -> None:
    """ORM维护入口不得删除唯一marker；downgrade由Alembic独占。"""

    raise ValueError("model tool loop schema marker cannot be deleted")


event.listen(
    ModelToolLoopSchemaMarkerModel,
    "before_update",
    _validate_model_tool_loop_marker_update,
)
event.listen(
    ModelToolLoopSchemaMarkerModel,
    "before_delete",
    _reject_model_tool_loop_marker_delete,
)


class ModelToolLoopModel(TimestampMixin, Base):
    """模型工具循环的耐久协调摘要，不复制usage、tool或context真相。"""

    __tablename__ = "model_tool_loops"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "loop_id",
            name="uq_model_tool_loops_tenant_loop",
        ),
        CheckConstraint(
            "length(id) > 0 and length(tenant_id) > 0 and length(run_id) > 0 "
            "and length(agent_id) > 0 and length(loop_id) = 64 "
            "and length(request_identity_digest) = 64 "
            "and length(operation_identity_digest) = 64 "
            "and length(catalog_digest) = 64",
            name="ck_model_tool_loops_identity_shape",
        ),
        CheckConstraint(
            "status in ('active','waiting_approval','completed','failed','cancelled',"
            "'needs_review')",
            name="ck_model_tool_loops_status",
        ),
        CheckConstraint(
            "next_turn_ordinal >= 1 and version >= 1 and owner_fence >= 1 "
            "and length(owner_lease_digest) = 64",
            name="ck_model_tool_loops_positive_state",
        ),
        CheckConstraint(
            "(status in ('active','waiting_approval') and result_ref is null "
            "and error_ref is null) or "
            "(status = 'completed' and result_ref is not null and length(result_ref) > 0 "
            "and error_ref is null) or "
            "(status in ('failed','cancelled','needs_review') and result_ref is null "
            "and error_ref is not null and length(error_ref) > 0)",
            name="ck_model_tool_loops_terminal_shape",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    loop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    next_turn_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_bounds_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cumulative_usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner_lease_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_fence: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    __mapper_args__ = {"version_id_col": version}


_MODEL_TOOL_LOOP_TRANSITIONS = {
    "active": frozenset(
        {"active", "waiting_approval", "completed", "failed", "cancelled", "needs_review"}
    ),
    "waiting_approval": frozenset(
        {"waiting_approval", "active", "failed", "cancelled", "needs_review"}
    ),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
    "needs_review": frozenset({"needs_review"}),
}


def _validate_model_tool_loop_transition(
    _mapper: Mapper[ModelToolLoopModel],
    _connection: Connection,
    target: ModelToolLoopModel,
) -> None:
    """在ORM flush前拒绝终态倒退；repository仍负责跨owner前置条件。"""

    history = inspect(target).attrs.status.history
    if not history.has_changes() or not history.deleted or not history.added:
        return
    previous = str(history.deleted[0])
    current = str(history.added[0])
    if current not in _MODEL_TOOL_LOOP_TRANSITIONS.get(previous, frozenset()):
        raise ValueError("model tool loop status transition is invalid")


def _mark_model_tool_loop_insert_evidence(
    _mapper: Mapper[ModelToolLoopModel],
    connection: Connection,
    _target: ModelToolLoopModel,
) -> None:
    """在loop行INSERT同一事务内先单调提升marker，缺失时拒绝写入。"""

    connection.execute(
        update(ModelToolLoopSchemaMarkerModel)
        .where(
            ModelToolLoopSchemaMarkerModel.marker_key == "model-tool-loop-v1",
            ModelToolLoopSchemaMarkerModel.evidence_seen.is_(False),
        )
        .values(evidence_seen=True)
    )
    evidence_seen = connection.scalar(
        select(ModelToolLoopSchemaMarkerModel.evidence_seen).where(
            ModelToolLoopSchemaMarkerModel.marker_key == "model-tool-loop-v1"
        )
    )
    if evidence_seen is not True:
        raise RuntimeError("storage.model_tool_loop_schema_marker_missing")


event.listen(ModelToolLoopModel, "before_insert", _mark_model_tool_loop_insert_evidence)
event.listen(ModelToolLoopModel, "before_update", _validate_model_tool_loop_transition)


class EmbeddingCacheModel(TimestampMixin, Base):
    """按 tenant/provider/model/input_hash 隔离的 embedding cache。"""

    __tablename__ = "tenant_embedding_cache"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "model",
            "input_hash",
            name="uq_tenant_embedding_cache_tenant_provider_model_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vector_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RunEventCapacityModel(Base):
    """每个 run 的 event high-water mark 与预约容量。"""

    __tablename__ = "run_event_capacity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_event_capacity_run_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "run_id", name="uq_run_event_capacity_tenant_run"),
        CheckConstraint(
            "highest_persisted_seq >= 0 AND outstanding_reserved_event_count >= 0 "
            "AND terminal_reservation IN (0, 1)",
            name="ck_run_event_capacity_non_negative",
        ),
        CheckConstraint(
            "highest_persisted_seq + outstanding_reserved_event_count "
            "+ terminal_reservation <= 2147483647",
            name="ck_run_event_capacity_total",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    highest_persisted_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outstanding_reserved_event_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    terminal_reservation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = [
    "TenantModel",
    "SessionModel",
    "AgentRunModel",
    "RunTraceBindingModel",
    "RunEventCapacityModel",
    "CheckpointModel",
    "ContextAssemblyModel",
    "ModelToolLoopModel",
    "ModelToolLoopSchemaMarkerModel",
    "EmbeddingCacheModel",
]
