"""增加模型工具循环协调表、兼容字段与不可逆evidence marker。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from alembic.script import ScriptDirectory

revision = "0018_model_tool_loop_state"
down_revision = "0017_model_route_chain_state"
branch_labels = None
depends_on = None

_MARKER_KEY = "model-tool-loop-v1"
_EMPTY_EVIDENCE_OPT_IN = "allow_empty_evidence_downgrade=true"
_SHARED_BUDGET_REVISION = "0016_shared_parent_budget_ledger"
_SHARED_BUDGET_EVIDENCE_TABLES = (
    "parent_budget_ledgers",
    "budget_operation_claims",
    "delegation_budget_allocations",
)


def upgrade() -> None:
    """创建0018协调骨架，并让0017工具/上下文记录保持nullable兼容。"""

    op.create_table(
        "model_tool_loop_schema_marker",
        sa.Column("marker_key", sa.String(length=64), primary_key=True),
        sa.Column(
            "evidence_seen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "marker_key = 'model-tool-loop-v1'",
            name="ck_model_tool_loop_schema_marker_key",
        ),
    )
    marker = sa.table(
        "model_tool_loop_schema_marker",
        sa.column("marker_key", sa.String(length=64)),
        sa.column("evidence_seen", sa.Boolean()),
    )
    op.bulk_insert(marker, [{"marker_key": _MARKER_KEY, "evidence_seen": False}])

    op.create_table(
        "model_tool_loops",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("loop_id", sa.String(length=64), nullable=False),
        sa.Column("request_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("operation_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("catalog_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("next_turn_ordinal", sa.Integer(), nullable=False),
        sa.Column("frozen_bounds_json", sa.JSON(), nullable=False),
        sa.Column("cumulative_usage_json", sa.JSON(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("result_ref", sa.String(length=512), nullable=True),
        sa.Column("error_ref", sa.String(length=512), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_lease_digest", sa.String(length=64), nullable=False),
        sa.Column("owner_fence", sa.Integer(), nullable=False),
        sa.Column(
            "owner_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "loop_id",
            name="uq_model_tool_loops_tenant_loop",
        ),
        sa.CheckConstraint(
            "length(id) > 0 and length(tenant_id) > 0 and length(run_id) > 0 "
            "and length(agent_id) > 0 and length(loop_id) = 64 "
            "and length(request_identity_digest) = 64 "
            "and length(operation_identity_digest) = 64 "
            "and length(catalog_digest) = 64",
            name="ck_model_tool_loops_identity_shape",
        ),
        sa.CheckConstraint(
            "status in ('active','waiting_approval','completed','failed','cancelled',"
            "'needs_review')",
            name="ck_model_tool_loops_status",
        ),
        sa.CheckConstraint(
            "next_turn_ordinal >= 1 and version >= 1 and owner_fence >= 1 "
            "and length(owner_lease_digest) = 64",
            name="ck_model_tool_loops_positive_state",
        ),
        sa.CheckConstraint(
            "(status in ('active','waiting_approval') and result_ref is null "
            "and error_ref is null) or "
            "(status = 'completed' and result_ref is not null and length(result_ref) > 0 "
            "and error_ref is null) or "
            "(status in ('failed','cancelled','needs_review') and result_ref is null "
            "and error_ref is not null and length(error_ref) > 0)",
            name="ck_model_tool_loops_terminal_shape",
        ),
    )
    op.create_index("ix_model_tool_loops_tenant_id", "model_tool_loops", ["tenant_id"])
    op.create_index("ix_model_tool_loops_run_id", "model_tool_loops", ["run_id"])
    op.create_index("ix_model_tool_loops_status", "model_tool_loops", ["status"])

    with op.batch_alter_table("tool_invocations") as batch:
        batch.add_column(sa.Column("loop_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("turn_ordinal", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tool_call_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("binding_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("execution_lease_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("execution_fence", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "execution_lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("handler_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("not_started_proof_json", sa.JSON(), nullable=True))
        batch.create_unique_constraint(
            "uq_tool_invocations_tool_call_id",
            ["tool_call_id"],
        )
        batch.create_check_constraint(
            "ck_tool_invocations_model_loop_shape",
            "(loop_id is null and turn_ordinal is null and tool_call_id is null "
            "and binding_json is null and execution_lease_digest is null "
            "and execution_fence is null and execution_lease_expires_at is null "
            "and handler_started_at is null and not_started_proof_json is null) or ("
            "loop_id is not null and turn_ordinal is not null and tool_call_id is not null "
            "and binding_json is not null and execution_lease_digest is not null "
            "and execution_fence is not null and execution_lease_expires_at is not null "
            "and length(loop_id) = 64 and turn_ordinal >= 1 and length(tool_call_id) = 64 "
            "and binding_json is not null and length(arguments_hash) = 64 "
            "and length(execution_lease_digest) = 64 and execution_fence >= 1 "
            "and execution_lease_expires_at is not null and run_id is not null "
            "and execution_state in ('claimed','executing','completed','failed','needs_review') "
            "and ((execution_state = 'claimed' and handler_started_at is null "
            "and result_ref is null) or (execution_state = 'executing' "
            "and handler_started_at is not null and result_ref is null) or "
            "(execution_state in ('completed','failed') and handler_started_at is not null "
            "and result_ref is not null and length(result_ref) > 0) or "
            "(execution_state = 'needs_review' and result_ref is null)))",
        )
    op.create_index(
        "ix_tool_invocations_loop_id",
        "tool_invocations",
        ["loop_id"],
    )

    with op.batch_alter_table("context_assemblies") as batch:
        batch.add_column(sa.Column("loop_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("turn_ordinal", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tool_call_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("input_identity_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("output_digest", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(
            "uq_context_assemblies_tenant_loop_turn",
            ["tenant_id", "loop_id", "turn_ordinal"],
        )
        batch.create_check_constraint(
            "ck_context_assemblies_model_loop_shape",
            "(loop_id is null and turn_ordinal is null and tool_call_id is null "
            "and input_identity_digest is null and output_digest is null) or ("
            "loop_id is not null and turn_ordinal is not null and tool_call_id is not null "
            "and input_identity_digest is not null and output_digest is not null "
            "and length(loop_id) = 64 and turn_ordinal >= 1 and length(tool_call_id) = 64 "
            "and length(input_identity_digest) = 64 and length(output_digest) = 64 "
            "and run_id is not null)",
        )
    op.create_index(
        "ix_context_assemblies_loop_id",
        "context_assemblies",
        ["loop_id"],
    )


def _downgrade_final_target() -> tuple[str, ...]:
    """解析完整downgrade路径的最终revision，供DDL前联合预检。"""

    destination = context.get_revision_argument()
    current = context.get_context().get_current_revision()
    if not isinstance(destination, str) or not isinstance(current, str) or current != revision:
        raise RuntimeError("0018 downgrade revision context is invalid")
    steps = ScriptDirectory.from_config(context.config)._downgrade_revs(  # pyright: ignore[reportPrivateUsage]
        destination,
        current,
    )
    if not steps or steps[0].from_revisions_no_deps != (revision,):
        raise RuntimeError("0018 downgrade path is invalid")
    return steps[-1].to_revisions_no_deps


def _preflight_older_revision_evidence(connection: sa.Connection) -> None:
    """目标越过0017/0016时，先执行其数据门禁，防止SQLite半降级。"""

    final_target = _downgrade_final_target()
    if final_target == (down_revision,):
        return
    for table_name in ("budget_operation_claims", "delegation_budget_allocations"):
        count = connection.execute(
            sa.text(
                f"select count(*) from {table_name} "
                "where route_chain_state_json is not null "
                "or identity_schema_version = 'budget-operation-v2'"
            )
        ).scalar_one()
        if count:
            raise RuntimeError("storage.route_chain_state_present")
    if final_target == (_SHARED_BUDGET_REVISION,):
        return
    if context.get_x_argument(as_dictionary=False) != [_EMPTY_EVIDENCE_OPT_IN]:
        raise RuntimeError("0016 downgrade requires explicit opt-in")
    for table_name in _SHARED_BUDGET_EVIDENCE_TABLES:
        count = connection.execute(sa.text(f"select count(*) from {table_name}")).scalar_one()
        if count:
            raise RuntimeError("0016 shared budget evidence exists; downgrade refused")


def downgrade() -> None:
    """在全部目标revision证据门禁通过后，才恢复0017 schema。"""

    connection = op.get_bind()
    marker = connection.execute(
        sa.text(
            "select evidence_seen from model_tool_loop_schema_marker where marker_key = :marker_key"
        ),
        {"marker_key": _MARKER_KEY},
    ).scalar_one_or_none()
    evidence_counts = (
        connection.execute(sa.text("select count(*) from model_tool_loops")).scalar_one(),
        connection.execute(
            sa.text(
                "select count(*) from tool_invocations "
                "where loop_id is not null or turn_ordinal is not null "
                "or tool_call_id is not null"
            )
        ).scalar_one(),
        connection.execute(
            sa.text(
                "select count(*) from context_assemblies "
                "where loop_id is not null or turn_ordinal is not null "
                "or tool_call_id is not null"
            )
        ).scalar_one(),
    )
    if marker is None or bool(marker) or any(int(count) for count in evidence_counts):
        raise RuntimeError("storage.model_tool_loop_evidence_present")
    _preflight_older_revision_evidence(connection)

    op.drop_index("ix_context_assemblies_loop_id", table_name="context_assemblies")
    with op.batch_alter_table("context_assemblies") as batch:
        batch.drop_constraint("ck_context_assemblies_model_loop_shape", type_="check")
        batch.drop_constraint("uq_context_assemblies_tenant_loop_turn", type_="unique")
        batch.drop_column("output_digest")
        batch.drop_column("input_identity_digest")
        batch.drop_column("tool_call_id")
        batch.drop_column("turn_ordinal")
        batch.drop_column("loop_id")
    op.drop_index("ix_tool_invocations_loop_id", table_name="tool_invocations")
    with op.batch_alter_table("tool_invocations") as batch:
        batch.drop_constraint("ck_tool_invocations_model_loop_shape", type_="check")
        batch.drop_constraint("uq_tool_invocations_tool_call_id", type_="unique")
        batch.drop_column("not_started_proof_json")
        batch.drop_column("handler_started_at")
        batch.drop_column("execution_lease_expires_at")
        batch.drop_column("execution_fence")
        batch.drop_column("execution_lease_digest")
        batch.drop_column("binding_json")
        batch.drop_column("tool_call_id")
        batch.drop_column("turn_ordinal")
        batch.drop_column("loop_id")
    op.drop_index("ix_model_tool_loops_status", table_name="model_tool_loops")
    op.drop_index("ix_model_tool_loops_run_id", table_name="model_tool_loops")
    op.drop_index("ix_model_tool_loops_tenant_id", table_name="model_tool_loops")
    op.drop_table("model_tool_loops")
    op.drop_table("model_tool_loop_schema_marker")
