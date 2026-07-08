"""创建 workspace 与工具调用持久化表。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0004_tool_execution_boundaries"
down_revision = "0003_auth_policy_hitl_approvals"
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
    """创建 Phase 8 工具执行边界需要的持久化表。"""

    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("policy_ref", sa.String(length=512), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *timestamp_columns(),
    )
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])
    op.create_index("ix_workspaces_agent_id", "workspaces", ["agent_id"])
    op.create_index("ix_workspaces_run_id", "workspaces", ["run_id"])

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("args_ref", sa.String(length=512), nullable=False),
        sa.Column("result_ref", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *timestamp_columns(),
    )
    op.create_index("ix_tool_invocations_tenant_id", "tool_invocations", ["tenant_id"])
    op.create_index("ix_tool_invocations_agent_id", "tool_invocations", ["agent_id"])
    op.create_index("ix_tool_invocations_run_id", "tool_invocations", ["run_id"])
    op.create_index("ix_tool_invocations_tool_name", "tool_invocations", ["tool_name"])
    op.create_index("ix_tool_invocations_status", "tool_invocations", ["status"])


def downgrade() -> None:
    """回滚 Phase 8 工具执行边界表。"""

    op.drop_table("tool_invocations")
    op.drop_table("workspaces")
