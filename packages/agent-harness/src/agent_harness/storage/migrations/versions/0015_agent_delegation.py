"""增加 delegation claim、预算预约与 durable aggregation。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0015_agent_delegation"
down_revision = "0014_run_evidence_outbox"
branch_labels = None
depends_on = None

_OPT_IN = "allow_empty_evidence_downgrade=true"
_EVIDENCE_TABLES = (
    "agent_delegations",
    "delegation_budget_reservations",
    "delegation_aggregates",
)


def upgrade() -> None:
    op.create_table(
        "agent_delegations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("parent_run_id", sa.String(36), nullable=False),
        sa.Column("child_run_id", sa.String(36), nullable=True),
        sa.Column("source_agent_id", sa.String(128), nullable=False),
        sa.Column("target_agent_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("budget_intent", sa.String(64), nullable=False),
        sa.Column("child_input_json", sa.JSON(), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="claimed"),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("event_operation_kind", sa.String(32), nullable=False),
        sa.Column("event_registry_version", sa.String(16), nullable=False),
        sa.Column("reserved_event_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "parent_run_id",
            "idempotency_key",
            name="uq_agent_delegations_parent_key",
        ),
        sa.UniqueConstraint("child_run_id", name="uq_agent_delegations_child_run"),
        sa.ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_agent_delegations_parent_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_agent_delegations_child_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status in ('claimed','queued','running','completed','failed',"
            "'released','needs_review')",
            name="ck_agent_delegations_status",
        ),
        sa.CheckConstraint(
            "event_operation_kind = 'delegation'",
            name="ck_agent_delegations_event_kind",
        ),
        sa.CheckConstraint(
            "reserved_event_count > 0",
            name="ck_agent_delegations_event_count",
        ),
    )
    op.create_index(
        "ix_agent_delegations_parent",
        "agent_delegations",
        ["tenant_id", "parent_run_id"],
    )
    op.create_table(
        "delegation_budget_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "delegation_id",
            sa.String(36),
            sa.ForeignKey("agent_delegations.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("parent_run_id", sa.String(36), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_usd", sa.Float(), nullable=True),
        sa.Column("settled_input_tokens", sa.Integer(), nullable=True),
        sa.Column("settled_output_tokens", sa.Integer(), nullable=True),
        sa.Column("settled_cost_usd", sa.Float(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_delegation_budget_parent_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("reserved_tokens >= 0", name="ck_delegation_budget_tokens"),
        sa.CheckConstraint(
            "reserved_cost_usd is null or reserved_cost_usd >= 0",
            name="ck_delegation_budget_cost",
        ),
        sa.CheckConstraint(
            "settled_input_tokens is null or settled_input_tokens >= 0",
            name="ck_delegation_budget_settled_input",
        ),
        sa.CheckConstraint(
            "settled_output_tokens is null or settled_output_tokens >= 0",
            name="ck_delegation_budget_settled_output",
        ),
        sa.CheckConstraint(
            "settled_cost_usd is null or settled_cost_usd >= 0",
            name="ck_delegation_budget_settled_cost",
        ),
        sa.CheckConstraint(
            "state != 'settled' or "
            "(settled_input_tokens is not null and settled_output_tokens is not null "
            "and settled_cost_usd is not null)",
            name="ck_delegation_budget_settled_complete",
        ),
        sa.CheckConstraint(
            "state in ('reserved','settled','released','needs_review')",
            name="ck_delegation_budget_state",
        ),
    )
    op.create_index(
        "ix_delegation_budget_parent",
        "delegation_budget_reservations",
        ["tenant_id", "parent_run_id"],
    )
    op.create_table(
        "delegation_aggregates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "delegation_id",
            sa.String(36),
            sa.ForeignKey("agent_delegations.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("parent_run_id", sa.String(36), nullable=False),
        sa.Column("child_run_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_delegation_aggregate_parent_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_delegation_aggregate_child_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status in ('complete','incomplete','needs_review')",
            name="ck_delegation_aggregate_status",
        ),
    )
    op.create_index(
        "ix_delegation_aggregates_parent",
        "delegation_aggregates",
        ["tenant_id", "parent_run_id"],
    )


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=False)
    if arguments != [_OPT_IN]:
        raise RuntimeError("0015 downgrade requires explicit opt-in")
    connection = op.get_bind()
    for table_name in _EVIDENCE_TABLES:
        count = connection.execute(sa.text(f"select count(*) from {table_name}")).scalar_one()
        if count:
            raise RuntimeError("0015 delegation evidence exists; downgrade refused")
    # 父子 run 关系落在 0001 已存在的表里，不依赖 delegation claim。它同样是
    # 0015 开始公开支持的 delegation 证据，必须在任何 drop DDL 前独立检查。
    related_run_count = connection.execute(
        sa.text("select count(*) from agent_runs where parent_run_id is not null")
    ).scalar_one()
    if related_run_count:
        raise RuntimeError("0015 delegation evidence exists; downgrade refused")

    op.drop_index("ix_delegation_aggregates_parent", table_name="delegation_aggregates")
    op.drop_table("delegation_aggregates")
    op.drop_index("ix_delegation_budget_parent", table_name="delegation_budget_reservations")
    op.drop_table("delegation_budget_reservations")
    op.drop_index("ix_agent_delegations_parent", table_name="agent_delegations")
    op.drop_table("agent_delegations")
