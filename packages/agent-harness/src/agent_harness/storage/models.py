"""核心持久化骨架的 SQLAlchemy typed models。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    """所有 ORM model 的 metadata 根。"""


class TimestampMixin:
    """带 created_at/updated_at 的通用时间戳列。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


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


class RetrievalDocumentModel(TimestampMixin, Base):
    """检索文档 evidence，保存来源、引用和 provider 无关 metadata。"""

    __tablename__ = "retrieval_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "collection",
            "document_id",
            name="uq_retrieval_documents_tenant_collection_document",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    collection: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    citation: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RetrievalChunkModel(TimestampMixin, Base):
    """检索 chunk evidence，保留 citation、trust 和 rank/vector metadata。"""

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "collection",
            "document_id",
            "chunk_id",
            name="uq_retrieval_chunks_tenant_collection_document_chunk",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    collection: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    chunk_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    citation: Mapped[str] = mapped_column(String(512), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(32), nullable=False, default="untrusted")
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vector_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rank_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    provider_metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )


class WorkspaceModel(TimestampMixin, Base):
    """工具执行可访问 workspace root 的持久化记录。"""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True, index=True
    )
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    policy_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ToolInvocationModel(TimestampMixin, Base):
    """一次工具调用的参数/result artifact 引用和状态摘要。"""

    __tablename__ = "tool_invocations"
    __table_args__ = (UniqueConstraint("approval_id", name="uq_tool_invocations_approval_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    args_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    arguments_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ApiKeyModel(TimestampMixin, Base):
    """API key 的 hash、角色和权限范围，不保存明文 token。"""

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_api_keys_token_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    roles_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    permissions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


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


class EvalCaseModel(TimestampMixin, Base):
    """eval case 的草稿/审核状态记录。"""

    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    trigger: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)
    source_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    artifact_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalRunModel(TimestampMixin, Base):
    """eval run 与源 run / case 的关联记录。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    dataset: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)
    eval_case_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("eval_cases.id"),
        nullable=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    score_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    score_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    provider_status_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")


class EvalScoreModel(TimestampMixin, Base):
    """一次 eval score 的本地 evidence 与 provider 写回摘要。"""

    __tablename__ = "eval_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    eval_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("eval_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("eval_cases.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    metric: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    provider_status_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )


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


class RunEvidenceOutboxModel(Base):
    """usage/approval/terminal evidence 的稳定 event-id settlement 状态。"""

    __tablename__ = "run_evidence_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_evidence_outbox_run_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "usage_call_id",
            name="uq_run_evidence_outbox_tenant_usage_call",
        ),
        UniqueConstraint("event_id", name="uq_run_evidence_outbox_event_id"),
        UniqueConstraint(
            "group_id",
            "sequence_in_group",
            name="uq_run_evidence_outbox_group_sequence",
        ),
        CheckConstraint(
            "(group_id IS NULL AND sequence_in_group IS NULL) OR "
            "(group_id IS NOT NULL AND sequence_in_group > 0)",
            name="ck_run_evidence_outbox_group_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    usage_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reserved_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    group_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sequence_in_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
