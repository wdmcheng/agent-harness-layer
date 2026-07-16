"""租户、会话、run、checkpoint 与上下文缓存 ORM。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

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
    "EmbeddingCacheModel",
]
