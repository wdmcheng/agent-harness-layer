"""扩展 eval gate 与 score sink 持久化 schema。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0007_eval_gate_trace_loop"
down_revision = "0006_retrieval_chunk_identity"
branch_labels = None
depends_on = None


def timestamp_columns() -> Sequence[sa.Column[Any]]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    """补齐 Phase 11 eval case/run/score 关联字段，不删除既有薄表数据。"""

    with op.batch_alter_table("eval_cases") as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("run_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("trace_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("trigger", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("dataset", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("source_refs_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("artifact_refs_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("metadata_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("approved_by", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("review_reason", sa.Text(), nullable=True))
    op.create_index("ix_eval_cases_agent_id", "eval_cases", ["agent_id"])
    op.create_index("ix_eval_cases_run_id", "eval_cases", ["run_id"])
    op.create_index("ix_eval_cases_trace_id", "eval_cases", ["trace_id"])
    op.create_index("ix_eval_cases_dataset", "eval_cases", ["dataset"])

    with op.batch_alter_table("eval_runs") as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("dataset", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("case_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("score_summary_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("provider_status_json", sa.JSON(), nullable=True))
    op.create_index("ix_eval_runs_agent_id", "eval_runs", ["agent_id"])
    op.create_index("ix_eval_runs_dataset", "eval_runs", ["dataset"])

    op.create_table(
        "eval_scores",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "eval_run_id",
            sa.String(length=36),
            sa.ForeignKey("eval_runs.id"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(length=36), sa.ForeignKey("eval_cases.id"), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("metric", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("provider_ref", sa.String(length=512), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("provider_status_json", sa.JSON(), nullable=False),
        *timestamp_columns(),
    )
    op.create_index("ix_eval_scores_tenant_id", "eval_scores", ["tenant_id"])
    op.create_index("ix_eval_scores_eval_run_id", "eval_scores", ["eval_run_id"])
    op.create_index("ix_eval_scores_case_id", "eval_scores", ["case_id"])
    op.create_index("ix_eval_scores_agent_id", "eval_scores", ["agent_id"])
    op.create_index("ix_eval_scores_run_id", "eval_scores", ["run_id"])
    op.create_index("ix_eval_scores_trace_id", "eval_scores", ["trace_id"])
    op.create_index("ix_eval_scores_metric", "eval_scores", ["metric"])


def downgrade() -> None:
    """回滚 Phase 11 schema 扩展，保留早期 eval_cases/eval_runs 薄表。"""

    op.drop_table("eval_scores")
    for index_name, table_name in [
        ("ix_eval_runs_dataset", "eval_runs"),
        ("ix_eval_runs_agent_id", "eval_runs"),
        ("ix_eval_cases_dataset", "eval_cases"),
        ("ix_eval_cases_trace_id", "eval_cases"),
        ("ix_eval_cases_run_id", "eval_cases"),
        ("ix_eval_cases_agent_id", "eval_cases"),
    ]:
        op.drop_index(index_name, table_name=table_name)
    with op.batch_alter_table("eval_runs") as batch_op:
        for column in [
            "provider_status_json",
            "score_summary_json",
            "case_count",
            "dataset",
            "agent_id",
        ]:
            batch_op.drop_column(column)
    with op.batch_alter_table("eval_cases") as batch_op:
        for column in [
            "review_reason",
            "approved_at",
            "approved_by",
            "metadata_json",
            "artifact_refs_json",
            "source_refs_json",
            "dataset",
            "trigger",
            "trace_id",
            "run_id",
            "agent_id",
        ]:
            batch_op.drop_column(column)
