"""增加 service API/worker 拆分所需的私有执行与事件字段。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_service_runtime_execution_context"
down_revision = "0011_eval_experiment_legacy_created_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 queued run、approval continuation 与 PostgreSQL event sink 建立真相源。"""

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("execution_context_json", sa.JSON()))
        batch_op.add_column(sa.Column("queue_operation_id", sa.String(length=512)))
        batch_op.add_column(sa.Column("queue_request_id", sa.String(length=128)))
        batch_op.add_column(sa.Column("queue_effective_idempotency_key", sa.String(length=512)))
        batch_op.add_column(sa.Column("queue_enqueue_state", sa.String(length=32)))
        batch_op.add_column(sa.Column("queue_message_id", sa.String(length=128)))
        batch_op.add_column(sa.Column("execution_owner_id", sa.String(length=512)))
        batch_op.add_column(sa.Column("execution_workflow_id", sa.String(length=512)))
    op.create_index("ix_agent_runs_queue_enqueue_state", "agent_runs", ["queue_enqueue_state"])
    op.create_index("ix_agent_runs_queue_operation_id", "agent_runs", ["queue_operation_id"])

    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("resolution_operation_id", sa.String(length=512)))
        batch_op.add_column(sa.Column("resolution_request_id", sa.String(length=128)))
        batch_op.add_column(sa.Column("resolution_reviewer_id", sa.String(length=255)))
        batch_op.add_column(sa.Column("resolution_decision", sa.String(length=32)))
        batch_op.add_column(sa.Column("resolution_request_hash", sa.String(length=64)))
        batch_op.add_column(sa.Column("resolution_comment", sa.Text()))
        batch_op.add_column(sa.Column("resolution_enqueue_state", sa.String(length=32)))
        batch_op.add_column(sa.Column("resolution_message_id", sa.String(length=128)))
        batch_op.add_column(sa.Column("resolution_workflow_owner_id", sa.String(length=512)))
        batch_op.add_column(sa.Column("resolution_workflow_id", sa.String(length=512)))
    op.create_index(
        "ix_approvals_resolution_enqueue_state",
        "approvals",
        ["resolution_enqueue_state"],
    )
    op.create_index(
        "ix_approvals_resolution_operation_id",
        "approvals",
        ["resolution_operation_id"],
    )

    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.String(length=36),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("envelope_json", sa.JSON()))
    op.create_index(
        "uq_canonical_events_run_terminal",
        "canonical_events",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("terminal = 1"),
        postgresql_where=sa.text("terminal is true"),
    )


def downgrade() -> None:
    """存在 service runtime durable execution/event evidence 时拒绝破坏性降级。"""

    connection = op.get_bind()
    counts = [
        int(
            connection.execute(
                sa.text(
                    """
                    select count(*) from agent_runs
                    where execution_context_json is not null
                       or queue_operation_id is not null
                       or queue_enqueue_state is not null
                       or execution_workflow_id is not null
                    """
                )
            ).scalar_one()
        ),
        int(
            connection.execute(
                sa.text(
                    """
                    select count(*) from approvals
                    where resolution_operation_id is not null
                       or resolution_enqueue_state is not null
                       or resolution_workflow_id is not null
                    """
                )
            ).scalar_one()
        ),
        int(
            connection.execute(
                sa.text("select count(*) from canonical_events where envelope_json is not null")
            ).scalar_one()
        ),
    ]
    if any(counts):
        raise RuntimeError("0012 downgrade refused: service runtime evidence exists")

    op.drop_index("uq_canonical_events_run_terminal", table_name="canonical_events")
    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.drop_column("envelope_json")
        batch_op.alter_column(
            "id",
            existing_type=sa.String(length=128),
            type_=sa.String(length=36),
            existing_nullable=False,
        )

    op.drop_index("ix_approvals_resolution_operation_id", table_name="approvals")
    op.drop_index("ix_approvals_resolution_enqueue_state", table_name="approvals")
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_column("resolution_workflow_id")
        batch_op.drop_column("resolution_workflow_owner_id")
        batch_op.drop_column("resolution_message_id")
        batch_op.drop_column("resolution_enqueue_state")
        batch_op.drop_column("resolution_comment")
        batch_op.drop_column("resolution_request_hash")
        batch_op.drop_column("resolution_decision")
        batch_op.drop_column("resolution_reviewer_id")
        batch_op.drop_column("resolution_request_id")
        batch_op.drop_column("resolution_operation_id")

    op.drop_index("ix_agent_runs_queue_operation_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_queue_enqueue_state", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_column("execution_workflow_id")
        batch_op.drop_column("execution_owner_id")
        batch_op.drop_column("queue_message_id")
        batch_op.drop_column("queue_enqueue_state")
        batch_op.drop_column("queue_effective_idempotency_key")
        batch_op.drop_column("queue_request_id")
        batch_op.drop_column("queue_operation_id")
        batch_op.drop_column("execution_context_json")
