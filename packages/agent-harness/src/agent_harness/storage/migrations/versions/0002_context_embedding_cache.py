"""创建 context assembly 与 embedding cache 表。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0002_context_embedding_cache"
down_revision = "0001_core_schema"
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
    """创建上下文组装 trace 与 embedding cache 表。"""

    op.create_table(
        "context_assemblies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("input_refs_json", sa.JSON(), nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("trust_summary_json", sa.JSON(), nullable=False),
        sa.Column("truncation_summary_json", sa.JSON(), nullable=False),
        sa.Column("output_ref", sa.String(length=512), nullable=False),
        *timestamp_columns(),
    )
    op.create_index("ix_context_assemblies_tenant_id", "context_assemblies", ["tenant_id"])
    op.create_table(
        "embedding_cache",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("vector_ref", sa.String(length=512), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "provider",
            "model",
            "input_hash",
            name="uq_embedding_cache_provider_model_hash",
        ),
    )
    op.create_index("ix_embedding_cache_tenant_id", "embedding_cache", ["tenant_id"])
    op.create_index("ix_embedding_cache_input_hash", "embedding_cache", ["input_hash"])


def downgrade() -> None:
    """回滚上下文组装 trace 与 embedding cache 表。"""

    op.drop_table("embedding_cache")
    op.drop_table("context_assemblies")
