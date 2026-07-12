"""为已有 eval experiment 表增加执行 claim 与租约。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_eval_experiment_execution_claims"
down_revision = "0009_eval_experiment_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只追加 nullable 私有状态，已有 experiment evidence 原样保留。"""

    with op.batch_alter_table("eval_experiments") as batch_op:
        batch_op.add_column(sa.Column("execution_claim_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column(
                "execution_claim_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """存在任何 experiment evidence 时拒绝删除执行安全字段。"""

    connection = op.get_bind()
    counts = {
        table: int(connection.execute(sa.text(f"select count(*) from {table}")).scalar_one())
        for table in (
            "eval_dataset_splits",
            "eval_experiments",
            "harness_acceptance_records",
        )
    }
    if any(counts.values()):
        raise RuntimeError("0010 downgrade refused: eval experiment evidence exists")

    with op.batch_alter_table("eval_experiments") as batch_op:
        batch_op.drop_column("execution_claim_expires_at")
        batch_op.drop_column("execution_claim_id")
