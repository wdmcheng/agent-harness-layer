"""检索、workspace、工具与 API key ORM。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.orm_base import Base as Base
from agent_harness.storage.orm_base import TimestampMixin as TimestampMixin


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
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_tool_invocations_approval_id"),
        UniqueConstraint("tool_call_id", name="uq_tool_invocations_tool_call_id"),
        CheckConstraint(
            "(loop_id is null and turn_ordinal is null and tool_call_id is null "
            "and binding_json is null and execution_lease_digest is null "
            "and execution_fence is null and execution_lease_expires_at is null "
            "and handler_started_at is null and not_started_proof_json is null) or ("
            "loop_id is not null and turn_ordinal is not null and tool_call_id is not null "
            "and binding_json is not null and execution_lease_digest is not null "
            "and execution_fence is not null and execution_lease_expires_at is not null "
            "and length(loop_id) = 64 and turn_ordinal >= 1 and length(tool_call_id) = 64 "
            "and binding_json is not null and length(arguments_hash) = 64 "
            "and length(execution_lease_digest) = 64 and execution_fence >= 1 "
            "and execution_lease_expires_at is not null and run_id is not null "
            "and execution_state in ('claimed','executing','completed','failed','needs_review') "
            "and ((execution_state = 'claimed' and handler_started_at is null "
            "and result_ref is null) or (execution_state = 'executing' "
            "and handler_started_at is not null and result_ref is null) or "
            "(execution_state in ('completed','failed') and handler_started_at is not null "
            "and result_ref is not null and length(result_ref) > 0) or "
            "(execution_state = 'needs_review' and result_ref is null)))",
            name="ck_tool_invocations_model_loop_shape",
        ),
    )

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
    loop_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    turn_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    binding_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True),
        nullable=True,
    )
    execution_lease_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_fence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    handler_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    not_started_proof_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True),
        nullable=True,
    )


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


__all__ = [
    "RetrievalDocumentModel",
    "RetrievalChunkModel",
    "WorkspaceModel",
    "ToolInvocationModel",
    "ApiKeyModel",
]
