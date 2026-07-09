"""创建 retrieval document 与 chunk evidence 表。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0005_retrieval_rag_foundation"
down_revision = "0004_tool_execution_boundaries"
branch_labels = None
depends_on = None


def timestamp_columns() -> Sequence[sa.Column[Any]]:
    """保持新增表和 ORM TimestampMixin 的列形状一致。"""

    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    """创建检索 evidence 表，用于保存 citation、source 和 rank metadata。"""

    op.create_table(
        "retrieval_documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("collection", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.String(length=255), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("citation", sa.String(length=512), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "collection",
            "document_id",
            name="uq_retrieval_documents_tenant_collection_document",
        ),
    )
    op.create_index("ix_retrieval_documents_tenant_id", "retrieval_documents", ["tenant_id"])
    op.create_index("ix_retrieval_documents_collection", "retrieval_documents", ["collection"])
    op.create_index("ix_retrieval_documents_document_id", "retrieval_documents", ["document_id"])

    op.create_table(
        "retrieval_chunks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("collection", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.String(length=255), nullable=False),
        sa.Column("chunk_id", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_ref", sa.String(length=512), nullable=True),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("citation", sa.String(length=512), nullable=False),
        sa.Column("trust_level", sa.String(length=32), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("vector_ref", sa.String(length=512), nullable=True),
        sa.Column("rank_metadata_json", sa.JSON(), nullable=False),
        sa.Column("provider_metadata_json", sa.JSON(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "collection",
            "chunk_id",
            name="uq_retrieval_chunks_tenant_collection_chunk",
        ),
    )
    op.create_index("ix_retrieval_chunks_tenant_id", "retrieval_chunks", ["tenant_id"])
    op.create_index("ix_retrieval_chunks_collection", "retrieval_chunks", ["collection"])
    op.create_index("ix_retrieval_chunks_document_id", "retrieval_chunks", ["document_id"])
    op.create_index("ix_retrieval_chunks_chunk_id", "retrieval_chunks", ["chunk_id"])


def downgrade() -> None:
    """回滚检索 evidence 表。"""

    op.drop_table("retrieval_chunks")
    op.drop_table("retrieval_documents")
