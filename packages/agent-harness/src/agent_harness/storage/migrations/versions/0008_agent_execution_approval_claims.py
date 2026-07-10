"""增加私有 approval lease 与 approved tool execution claim。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_agent_execution_approval_claims"
down_revision = "0007_eval_gate_trace_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """持久化 resolution 仲裁状态，但不扩展 public approval status。"""

    # Alembic 默认 version_num 是 VARCHAR(32)，而本仓库使用可读 revision 名。
    # PostgreSQL 会严格拒绝超长值，SQLite 则不会暴露该问题；先扩容再让
    # Alembic 在 migration 返回后写入当前 revision。该元数据列保持 64，
    # disposable downgrade 也不缩回，避免在 version update 前截断当前值。
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("resolution_lease_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("resolution_state", sa.String(length=32)))
        batch_op.add_column(sa.Column("resolution_claimed_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("resolution_finalized_at", sa.DateTime(timezone=True)))
    op.create_index("ix_approvals_resolution_lease_id", "approvals", ["resolution_lease_id"])
    op.create_index("ix_approvals_resolution_state", "approvals", ["resolution_state"])

    with op.batch_alter_table("tool_invocations") as batch_op:
        batch_op.add_column(sa.Column("approval_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("arguments_hash", sa.String(length=64)))
        batch_op.add_column(sa.Column("execution_state", sa.String(length=32)))
        batch_op.create_unique_constraint(
            "uq_tool_invocations_approval_id",
            ["approval_id"],
        )
    op.create_index("ix_tool_invocations_approval_id", "tool_invocations", ["approval_id"])


def downgrade() -> None:
    """仅允许 disposable 空数据环境移除 private resolution/claim schema。"""

    connection = op.get_bind()
    approval_resolution_rows = int(
        connection.execute(
            sa.text(
                """
                select count(*)
                from approvals
                where resolution_lease_id is not null
                   or resolution_state is not null
                   or resolution_claimed_at is not null
                   or resolution_finalized_at is not null
                """
            )
        ).scalar_one()
    )
    tool_claim_rows = int(
        connection.execute(
            sa.text(
                """
                select count(*)
                from tool_invocations
                where approval_id is not null
                   or arguments_hash is not null
                   or execution_state is not null
                """
            )
        ).scalar_one()
    )
    if approval_resolution_rows or tool_claim_rows:
        raise RuntimeError(
            "0008 downgrade refused: approval resolution or tool execution claim data exists"
        )

    op.drop_index("ix_tool_invocations_approval_id", table_name="tool_invocations")
    with op.batch_alter_table("tool_invocations") as batch_op:
        batch_op.drop_constraint("uq_tool_invocations_approval_id", type_="unique")
        batch_op.drop_column("execution_state")
        batch_op.drop_column("arguments_hash")
        batch_op.drop_column("approval_id")

    op.drop_index("ix_approvals_resolution_state", table_name="approvals")
    op.drop_index("ix_approvals_resolution_lease_id", table_name="approvals")
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_column("resolution_finalized_at")
        batch_op.drop_column("resolution_claimed_at")
        batch_op.drop_column("resolution_state")
        batch_op.drop_column("resolution_lease_id")
