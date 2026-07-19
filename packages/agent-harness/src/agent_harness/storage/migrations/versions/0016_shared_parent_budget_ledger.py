"""增加 execution-tree shared parent budget ledger。"""

# Alembic 的 JSON checkpoint 与动态 table clause 在逐字段运行时校验后才使用；
# SQLAlchemy stubs 无法把这些 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.apply import (
    _apply_backfill,
    _legacy_preflight,
)

revision = "0016_shared_parent_budget_ledger"
down_revision = "0015_agent_delegation"
branch_labels = None
depends_on = None

_OPT_IN = "allow_empty_evidence_downgrade=true"
_EVIDENCE_TABLES = (
    "delegation_budget_allocations",
    "budget_operation_claims",
    "parent_budget_ledgers",
)


def _identity_text(key: str) -> sa.ColumnElement[str]:
    """生成由 SQLite/PostgreSQL 各自编译的 JSON 文本字段表达式。"""

    return sa.column("identity_json", sa.JSON())[key].as_string()


def _required_identity_equal(key: str, value: object) -> sa.ColumnElement[bool]:
    field = _identity_text(key)
    return sa.and_(field.is_not(None), field == value)


def _required_identity_text(key: str) -> sa.ColumnElement[bool]:
    field = _identity_text(key)
    return sa.and_(field.is_not(None), sa.func.length(field) > 0)


def _claim_identity_json_shape() -> sa.ColumnElement[bool]:
    operation_kind = sa.column("operation_kind", sa.String())
    usage_kind = sa.column("usage_kind", sa.String())
    schema_version = sa.column("identity_schema_version", sa.String())
    identity_hash = sa.column("identity_hash", sa.String())
    run_id = sa.column("run_id", sa.String())
    agent_id = sa.column("agent_id", sa.String())
    delegation_id = sa.column("delegation_id", sa.String())
    return sa.and_(
        _required_identity_equal("ownership_kind", operation_kind),
        _required_identity_equal("usage_kind", usage_kind),
        _required_identity_equal("identity_schema_version", schema_version),
        _required_identity_equal("identity_hash", identity_hash),
        _required_identity_equal("run_id", run_id),
        _required_identity_equal("agent_id", agent_id),
        _required_identity_text("request_fingerprint"),
        _required_identity_text("fingerprint_key_version"),
        sa.or_(
            sa.and_(
                operation_kind == "direct",
                _identity_text("delegation_claim_id").is_(None),
                _identity_text("source_agent_id").is_(None),
                _identity_text("target_agent_id").is_(None),
                _identity_text("target_route_catalog_digest").is_(None),
            ),
            sa.and_(
                operation_kind == "delegation",
                _required_identity_equal("delegation_claim_id", delegation_id),
                _required_identity_equal("source_agent_id", agent_id),
                _required_identity_text("target_agent_id"),
                _required_identity_text("target_route_catalog_digest"),
            ),
        ),
    )


def _allocation_identity_json_shape() -> sa.ColumnElement[bool]:
    return sa.and_(
        _required_identity_equal("ownership_kind", "allocation"),
        _required_identity_equal("usage_kind", sa.column("usage_kind", sa.String())),
        _required_identity_equal(
            "identity_schema_version", sa.column("identity_schema_version", sa.String())
        ),
        _required_identity_equal("identity_hash", sa.column("identity_hash", sa.String())),
        _required_identity_equal("run_id", sa.column("run_id", sa.String())),
        _required_identity_equal("agent_id", sa.column("agent_id", sa.String())),
        _required_identity_equal("delegation_claim_id", sa.column("delegation_id", sa.String())),
        _identity_text("source_agent_id").is_(None),
        _identity_text("target_agent_id").is_(None),
        _identity_text("target_route_catalog_digest").is_(None),
        _required_identity_text("request_fingerprint"),
        _required_identity_text("fingerprint_key_version"),
    )


def upgrade() -> None:
    connection = op.get_bind()
    backfill_plans = _legacy_preflight(connection)
    op.create_table(
        "parent_budget_ledgers",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("budget_owner_run_id", sa.String(36), primary_key=True),
        sa.Column("token_limit", sa.Integer(), nullable=False),
        sa.Column("cost_limit", sa.Numeric(20, 8), nullable=True),
        sa.Column("cost_enabled", sa.Boolean(), nullable=False),
        sa.Column("token_impact", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_impact", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("registry_version", sa.String(128), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.Column("catalog_version", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(255), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
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
            ["budget_owner_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_parent_budget_ledger_owner_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "snapshot_id", name="uq_parent_budget_snapshot"),
        sa.CheckConstraint("token_limit >= 0", name="ck_parent_budget_token_limit"),
        sa.CheckConstraint("token_impact >= 0", name="ck_parent_budget_token_impact"),
        sa.CheckConstraint("cost_impact >= 0", name="ck_parent_budget_cost_impact"),
        sa.CheckConstraint(
            "(cost_enabled = false and cost_limit is null and cost_impact = 0) or "
            "(cost_enabled = true and cost_limit is not null)",
            name="ck_parent_budget_cost_mode",
        ),
        sa.CheckConstraint(
            "state in ('active','needs_review','terminal')",
            name="ck_parent_budget_state",
        ),
    )
    op.create_table(
        "budget_operation_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("budget_owner_run_id", sa.String(36), nullable=False),
        sa.Column("operation_kind", sa.String(16), nullable=False),
        sa.Column("usage_call_id", sa.String(64), nullable=True),
        sa.Column("delegation_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("usage_kind", sa.String(16), nullable=False),
        sa.Column("identity_schema_version", sa.String(64), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost", sa.Numeric(20, 8), nullable=True),
        sa.Column("actual_tokens", sa.Integer(), nullable=True),
        sa.Column("actual_cost", sa.Numeric(20, 8), nullable=True),
        sa.Column("token_impact", sa.Integer(), nullable=False),
        sa.Column("cost_impact", sa.Numeric(20, 8), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("side_effect_state", sa.String(24), nullable=False, server_default="not_started"),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("backfill_source", sa.String(64), nullable=True),
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
            ["tenant_id", "budget_owner_run_id"],
            ["parent_budget_ledgers.tenant_id", "parent_budget_ledgers.budget_owner_run_id"],
            name="fk_budget_claim_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_budget_claim_run_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delegation_id"], ["agent_delegations.id"], name="fk_budget_claim_delegation"
        ),
        sa.UniqueConstraint(
            "tenant_id", "budget_owner_run_id", "usage_call_id", name="uq_budget_claim_direct_usage"
        ),
        sa.UniqueConstraint("delegation_id", name="uq_budget_claim_delegation"),
        sa.CheckConstraint(
            "(operation_kind = 'direct' and usage_call_id is not null "
            "and delegation_id is null) or "
            "(operation_kind = 'delegation' and usage_call_id is null "
            "and delegation_id is not null)",
            name="ck_budget_claim_kind_key",
        ),
        sa.CheckConstraint(
            "(operation_kind = 'direct' and usage_kind in ('model','embedding') "
            "and identity_schema_version = 'budget-operation-v1' and request_hash is null) or "
            "(operation_kind = 'delegation' and usage_kind = 'delegation' "
            "and identity_schema_version = 'budget-delegation-v1' and request_hash is not null)",
            name="ck_budget_claim_identity_shape",
        ),
        sa.CheckConstraint(
            _claim_identity_json_shape(),
            name="ck_budget_claim_identity_json_shape",
        ),
        sa.CheckConstraint(
            "state in ('reserved','settled','released','needs_review')",
            name="ck_budget_claim_state",
        ),
        sa.CheckConstraint(
            "side_effect_state in ('not_started','started','result_committed')",
            name="ck_budget_claim_side_effect",
        ),
        sa.CheckConstraint(
            "reserved_tokens >= 0 and token_impact >= 0", name="ck_budget_claim_tokens"
        ),
        sa.CheckConstraint(
            "reserved_cost is null or reserved_cost >= 0", name="ck_budget_claim_reserved_cost"
        ),
        sa.CheckConstraint("cost_impact >= 0", name="ck_budget_claim_cost_impact"),
    )
    op.create_index(
        "ix_budget_claim_owner_state",
        "budget_operation_claims",
        ["tenant_id", "budget_owner_run_id", "state"],
    )
    op.create_table(
        "delegation_budget_allocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("budget_owner_run_id", sa.String(36), nullable=False),
        sa.Column("delegation_id", sa.String(36), nullable=False),
        sa.Column("usage_call_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("usage_kind", sa.String(16), nullable=False),
        sa.Column("identity_schema_version", sa.String(64), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=True),
        sa.Column("reserved_cost", sa.Numeric(20, 8), nullable=True),
        sa.Column("actual_tokens", sa.Integer(), nullable=True),
        sa.Column("actual_cost", sa.Numeric(20, 8), nullable=True),
        sa.Column("token_impact", sa.Integer(), nullable=False),
        sa.Column("cost_impact", sa.Numeric(20, 8), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("side_effect_state", sa.String(24), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("backfill_source", sa.String(64), nullable=True),
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
            ["tenant_id", "budget_owner_run_id"],
            ["parent_budget_ledgers.tenant_id", "parent_budget_ledgers.budget_owner_run_id"],
            name="fk_budget_allocation_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_budget_allocation_run_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delegation_id"], ["agent_delegations.id"], name="fk_budget_allocation_delegation"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "budget_owner_run_id",
            "delegation_id",
            "usage_call_id",
            name="uq_budget_allocation_usage",
        ),
        sa.CheckConstraint(
            "usage_kind in ('model','embedding') "
            "and identity_schema_version = 'budget-operation-v1'",
            name="ck_budget_allocation_identity_shape",
        ),
        sa.CheckConstraint(
            _allocation_identity_json_shape(),
            name="ck_budget_allocation_identity_json_shape",
        ),
        sa.CheckConstraint(
            "state in ('reserved','settled','released','needs_review')",
            name="ck_budget_allocation_state",
        ),
        sa.CheckConstraint(
            "side_effect_state in ('not_started','started','result_committed')",
            name="ck_budget_allocation_side_effect",
        ),
        sa.CheckConstraint(
            "reserved_tokens is null or reserved_tokens >= 0",
            name="ck_budget_allocation_reserved_tokens",
        ),
        sa.CheckConstraint(
            "token_impact >= 0 and cost_impact >= 0", name="ck_budget_allocation_impact"
        ),
    )
    op.create_index(
        "ix_budget_allocation_delegation",
        "delegation_budget_allocations",
        ["delegation_id", "state"],
    )
    _apply_backfill(connection, backfill_plans)


def downgrade() -> None:
    arguments = context.get_x_argument(as_dictionary=False)
    if arguments != [_OPT_IN]:
        raise RuntimeError("0016 downgrade requires explicit opt-in")
    connection = op.get_bind()
    for table_name in _EVIDENCE_TABLES:
        count = connection.execute(sa.text(f"select count(*) from {table_name}")).scalar_one()
        if count:
            raise RuntimeError("0016 shared budget evidence exists; downgrade refused")
    op.drop_index("ix_budget_allocation_delegation", table_name="delegation_budget_allocations")
    op.drop_table("delegation_budget_allocations")
    op.drop_index("ix_budget_claim_owner_state", table_name="budget_operation_claims")
    op.drop_table("budget_operation_claims")
    op.drop_table("parent_budget_ledgers")
