"""创建 eval dataset split、experiment 与人工 acceptance 表。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0009_eval_experiment_loop"
down_revision = "0008_agent_execution_approval_claims"
branch_labels = None
depends_on = None


def _timestamp_columns() -> Sequence[sa.Column[Any]]:
    """集中声明 experiment 相关新表的时间戳列，防止表间审计字段语义分叉。"""

    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    """建立 eval experiment 唯一 schema，不回填或改写既有 eval case。"""

    op.create_table(
        "eval_dataset_splits",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("dataset", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("optimization_ratio", sa.Float(), nullable=False),
        sa.Column("holdout_ratio", sa.Float(), nullable=False),
        sa.Column("regression_policy_json", sa.JSON(), nullable=False),
        sa.Column("case_tags_json", sa.JSON(), nullable=False),
        sa.Column("optimization_case_ids_json", sa.JSON(), nullable=False),
        sa.Column("holdout_case_ids_json", sa.JSON(), nullable=False),
        sa.Column("regression_case_ids_json", sa.JSON(), nullable=False),
        sa.Column("optimization_case_count", sa.Integer(), nullable=False),
        sa.Column("holdout_case_count", sa.Integer(), nullable=False),
        sa.Column("regression_case_count", sa.Integer(), nullable=False),
        sa.Column("tag_distribution_json", sa.JSON(), nullable=False),
        sa.Column("rejected_counts_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_eval_dataset_splits_tenant_id",
        ),
    )
    op.create_index("ix_eval_dataset_splits_tenant_id", "eval_dataset_splits", ["tenant_id"])
    op.create_index("ix_eval_dataset_splits_agent_id", "eval_dataset_splits", ["agent_id"])
    op.create_index("ix_eval_dataset_splits_dataset", "eval_dataset_splits", ["dataset"])

    op.create_table(
        "eval_experiments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("dataset", sa.String(length=255), nullable=False),
        sa.Column("split_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("evaluator_profile_json", sa.JSON(), nullable=False),
        sa.Column("metric_versions_json", sa.JSON(), nullable=False),
        sa.Column("baseline_harness_json", sa.JSON(), nullable=False),
        sa.Column("candidate_harness_json", sa.JSON(), nullable=True),
        sa.Column("baseline_run_ref", sa.String(length=512), nullable=True),
        sa.Column("candidate_run_ref", sa.String(length=512), nullable=True),
        sa.Column("score_summaries_json", sa.JSON(), nullable=False),
        sa.Column("comparison_json", sa.JSON(), nullable=False),
        sa.Column("local_refs_json", sa.JSON(), nullable=False),
        sa.Column("provider_status_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_eval_experiments_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_eval_experiments_tenant_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "split_id"],
            ["eval_dataset_splits.tenant_id", "eval_dataset_splits.id"],
            name="fk_eval_experiments_tenant_split",
        ),
    )
    op.create_index("ix_eval_experiments_tenant_id", "eval_experiments", ["tenant_id"])
    op.create_index("ix_eval_experiments_agent_id", "eval_experiments", ["agent_id"])
    op.create_index("ix_eval_experiments_dataset", "eval_experiments", ["dataset"])
    op.create_index("ix_eval_experiments_split_id", "eval_experiments", ["split_id"])
    op.create_index("ix_eval_experiments_status", "eval_experiments", ["status"])

    op.create_table(
        "harness_acceptance_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("decision_request_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewer_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("accepted_harness_version", sa.String(length=64), nullable=True),
        sa.Column("production_binding_json", sa.JSON(), nullable=True),
        sa.Column("policy_decision_json", sa.JSON(), nullable=False),
        sa.Column("audit_ref", sa.String(length=512), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("followup_issue_ref", sa.String(length=512), nullable=True),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "experiment_id",
            name="uq_harness_acceptance_tenant_experiment",
        ),
        sa.UniqueConstraint(
            "experiment_id",
            name="uq_harness_acceptance_experiment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "experiment_id"],
            ["eval_experiments.tenant_id", "eval_experiments.id"],
            name="fk_harness_acceptance_tenant_experiment",
        ),
    )
    op.create_index(
        "ix_harness_acceptance_records_tenant_id",
        "harness_acceptance_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_harness_acceptance_records_experiment_id",
        "harness_acceptance_records",
        ["experiment_id"],
    )


def downgrade() -> None:
    """只允许三张表全部为空的 disposable 环境回退。"""

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
        raise RuntimeError("0009 downgrade refused: eval experiment evidence exists")

    op.drop_table("harness_acceptance_records")
    op.drop_table("eval_experiments")
    op.drop_table("eval_dataset_splits")
