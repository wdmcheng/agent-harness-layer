"""建立 canonical run trace binding 并回填 run-scoped evidence。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

from agent_harness.storage.migrations.versions._run_trace_correlation_0013.backfill import (
    apply_backfill,
    build_backfill_plan,
)

revision = "0013_run_trace_correlation"
down_revision = "0012a_embedding_cache_tenant_scope"
branch_labels = None
depends_on = None

_OPT_IN = "allow_empty_evidence_downgrade=true"


def upgrade() -> None:
    """完整预检 lineage 后再创建 binding、约束并回填关联 evidence。"""

    connection = op.get_bind()
    plan = build_backfill_plan(connection)

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("trace_id", sa.String(length=128), nullable=True))
        batch_op.create_unique_constraint("uq_agent_runs_id_tenant", ["id", "tenant_id"])
        batch_op.create_unique_constraint(
            "uq_agent_runs_id_tenant_trace",
            ["id", "tenant_id", "trace_id"],
        )
        batch_op.create_foreign_key(
            "fk_agent_runs_parent_tenant",
            "agent_runs",
            ["parent_run_id", "tenant_id"],
            ["id", "tenant_id"],
            deferrable=True,
            initially="DEFERRED",
        )
    op.create_index("ix_agent_runs_trace_id", "agent_runs", ["trace_id"])
    op.create_table(
        "run_trace_bindings",
        sa.Column("trace_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("root_run_id", sa.String(length=36), nullable=False),
        sa.UniqueConstraint("root_run_id", name="uq_run_trace_bindings_root_run_id"),
        sa.UniqueConstraint(
            "trace_id",
            "tenant_id",
            name="uq_run_trace_bindings_trace_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["root_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_trace_bindings_root_tenant",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    with op.batch_alter_table("trace_refs") as batch_op:
        batch_op.add_column(sa.Column("trace_id", sa.String(length=128), nullable=True))
    op.create_index("ix_trace_refs_trace_id", "trace_refs", ["trace_id"])
    with op.batch_alter_table("eval_runs") as batch_op:
        batch_op.add_column(sa.Column("trace_id", sa.String(length=128), nullable=True))
    op.create_index("ix_eval_runs_trace_id", "eval_runs", ["trace_id"])
    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.drop_constraint("uq_canonical_events_run_seq", type_="unique")
        batch_op.alter_column(
            "run_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.add_column(sa.Column("stream_id", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column("record_scope", sa.String(length=16), nullable=False, server_default="run")
        )
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "record_scope", sa.String(length=16), nullable=False, server_default="non_run"
            )
        )

    apply_backfill(connection, plan)

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.create_foreign_key(
            "fk_agent_runs_trace_binding_tenant",
            "run_trace_bindings",
            ["trace_id", "tenant_id"],
            ["trace_id", "tenant_id"],
            deferrable=True,
            initially="DEFERRED",
        )

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.alter_column(
            "trace_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.alter_column(
            "trace_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.alter_column(
            "stream_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_canonical_events_tenant_stream_seq",
            ["tenant_id", "stream_id", "seq"],
        )
        batch_op.create_check_constraint(
            "ck_canonical_events_record_scope",
            "record_scope IN ('run', 'non_run')",
        )
        batch_op.create_check_constraint(
            "ck_canonical_events_run_ownership",
            "record_scope != 'run' OR (run_id IS NOT NULL AND trace_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_canonical_events_non_run_ownership",
            "record_scope != 'non_run' OR run_id IS NULL",
        )
        batch_op.create_foreign_key(
            "fk_canonical_events_run_owner",
            "agent_runs",
            ["run_id", "tenant_id", "trace_id"],
            ["id", "tenant_id", "trace_id"],
            deferrable=True,
            initially="DEFERRED",
        )
    op.create_index("ix_canonical_events_stream_id", "canonical_events", ["stream_id"])
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.create_check_constraint(
            "ck_audit_logs_record_scope",
            "record_scope IN ('run', 'non_run')",
        )


def downgrade() -> None:
    """只有空 evidence 且精确 opt-in 时恢复 0012a trace-nullable schema。"""

    _require_exact_empty_evidence_opt_in()
    connection = op.get_bind()
    counts = [
        int(connection.execute(sa.text("select count(*) from run_trace_bindings")).scalar_one()),
        int(connection.execute(sa.text("select count(*) from agent_runs")).scalar_one()),
        int(
            connection.execute(
                sa.text("select count(*) from approvals where trace_id is not null")
            ).scalar_one()
        ),
        int(
            connection.execute(
                sa.text("select count(*) from canonical_events where trace_id is not null")
            ).scalar_one()
        ),
        int(
            connection.execute(
                sa.text("select count(*) from trace_refs where trace_id is not null")
            ).scalar_one()
        ),
        int(
            connection.execute(
                sa.text("select count(*) from eval_runs where trace_id is not null")
            ).scalar_one()
        ),
    ]
    if any(counts):
        raise RuntimeError("0013 downgrade refused: canonical trace evidence exists")

    op.drop_index("ix_canonical_events_stream_id", table_name="canonical_events")
    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.drop_constraint("fk_canonical_events_run_owner", type_="foreignkey")
        batch_op.drop_constraint("ck_canonical_events_non_run_ownership", type_="check")
        batch_op.drop_constraint("ck_canonical_events_run_ownership", type_="check")
        batch_op.drop_constraint("ck_canonical_events_record_scope", type_="check")
        batch_op.drop_constraint("uq_canonical_events_tenant_stream_seq", type_="unique")
        batch_op.alter_column(
            "trace_id",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.drop_column("record_scope")
        batch_op.drop_column("stream_id")
        batch_op.alter_column(
            "run_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_canonical_events_run_seq",
            ["run_id", "seq"],
        )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.alter_column(
            "trace_id",
            existing_type=sa.String(length=128),
            nullable=True,
        )
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_constraint("ck_audit_logs_record_scope", type_="check")
        batch_op.drop_column("record_scope")
    op.drop_index("ix_eval_runs_trace_id", table_name="eval_runs")
    with op.batch_alter_table("eval_runs") as batch_op:
        batch_op.drop_column("trace_id")
    op.drop_index("ix_trace_refs_trace_id", table_name="trace_refs")
    with op.batch_alter_table("trace_refs") as batch_op:
        batch_op.drop_column("trace_id")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("fk_agent_runs_trace_binding_tenant", type_="foreignkey")
    op.drop_table("run_trace_bindings")
    op.drop_index("ix_agent_runs_trace_id", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("fk_agent_runs_parent_tenant", type_="foreignkey")
        batch_op.drop_constraint("uq_agent_runs_id_tenant_trace", type_="unique")
        batch_op.drop_constraint("uq_agent_runs_id_tenant", type_="unique")
        batch_op.drop_column("trace_id")


def _require_exact_empty_evidence_opt_in() -> None:
    """仅接受单个精确 opt-in 参数，防止模糊开关误触破坏性 evidence downgrade。"""

    arguments = context.get_x_argument(as_dictionary=False)
    matches = [item for item in arguments if item.startswith("allow_empty_evidence_downgrade")]
    if matches != [_OPT_IN]:
        raise RuntimeError("0013 downgrade refused: explicit opt-in is required")
