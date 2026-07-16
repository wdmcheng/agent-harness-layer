"""事件、trace、artifact、策略、审批与审计 ORM。"""

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.orm_base import Base as Base
from agent_harness.storage.orm_base import TimestampMixin as TimestampMixin


class CanonicalEventModel(Base):
    """CanonicalEvent 的数据库持久化形状。"""

    __tablename__ = "canonical_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "stream_id",
            "seq",
            name="uq_canonical_events_tenant_stream_seq",
        ),
        CheckConstraint(
            "record_scope IN ('run', 'non_run')",
            name="ck_canonical_events_record_scope",
        ),
        CheckConstraint(
            "record_scope != 'run' OR (run_id IS NOT NULL AND trace_id IS NOT NULL)",
            name="ck_canonical_events_run_ownership",
        ),
        CheckConstraint(
            "record_scope != 'non_run' OR run_id IS NULL",
            name="ck_canonical_events_non_run_ownership",
        ),
        ForeignKeyConstraint(
            ["run_id", "tenant_id", "trace_id"],
            ["agent_runs.id", "agent_runs.tenant_id", "agent_runs.trace_id"],
            name="fk_canonical_events_run_owner",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    # DB run_id 只表示真实 AgentRun ownership；non-run telemetry 的合成
    # envelope run_id 保存在 stream_id/envelope_json，不能伪造 lineage。
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True, index=True
    )
    stream_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    record_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="run")
    envelope_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TraceRefModel(TimestampMixin, Base):
    """外部观测 provider trace 的引用表。"""

    __tablename__ = "trace_refs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_trace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class ArtifactModel(TimestampMixin, Base):
    """大 payload 或文件 artifact 的元数据记录。"""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PolicyRuleModel(TimestampMixin, Base):
    """DB policy provider 可读取的单条策略规则。"""

    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ApprovalModel(TimestampMixin, Base):
    """HITL approval 状态机的持久化记录。"""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting", index=True)
    resume_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    resolution_state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    resolution_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_operation_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    resolution_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_reviewer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolution_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_enqueue_state: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    resolution_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_workflow_owner_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resolution_workflow_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AuditLogModel(Base):
    """policy、approval、认证和危险动作的结构化审计记录。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "record_scope IN ('run', 'non_run')",
            name="ck_audit_logs_record_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    record_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="non_run")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = [
    "CanonicalEventModel",
    "TraceRefModel",
    "ArtifactModel",
    "PolicyRuleModel",
    "ApprovalModel",
    "AuditLogModel",
]
