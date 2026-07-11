"""把旧版结果不确定的 created experiment 收敛到人工复核。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_eval_experiment_legacy_created_review"
down_revision = "0010_eval_experiment_execution_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧 0009 created 可能已调用 evaluator，禁止升级后自动重跑。"""

    op.execute(
        sa.text(
            """
            update eval_experiments
            set status = 'needs_review'
            where status = 'created'
              and execution_claim_id is null
            """
        )
    )


def downgrade() -> None:
    """任一 Phase 12.5 evidence 存在时拒绝回到会重跑 created 的旧代码。"""

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
        raise RuntimeError("0011 downgrade refused: Phase 12.5 eval evidence exists")
