"""为 shared-budget direct/allocation 增加显式 route-chain v2 状态。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from alembic.script import ScriptDirectory

from agent_harness.storage.shared_budget_models import (
    allocation_identity_json_shape,
    claim_identity_json_shape,
)

revision = "0017_model_route_chain_state"
down_revision = "0016_shared_parent_budget_ledger"
branch_labels = None
depends_on = None

_CLAIM_V2_SHAPE = (
    "(operation_kind = 'direct' and usage_kind in ('model','embedding') "
    "and identity_schema_version in ('budget-operation-v1','budget-operation-v2') "
    "and not (identity_schema_version = 'budget-operation-v2' and usage_kind != 'model') "
    "and request_hash is null) or "
    "(operation_kind = 'delegation' and usage_kind = 'delegation' "
    "and identity_schema_version = 'budget-delegation-v1' and request_hash is not null)"
)
_ALLOCATION_V2_SHAPE = (
    "usage_kind in ('model','embedding') "
    "and identity_schema_version in ('budget-operation-v1','budget-operation-v2') "
    "and not (identity_schema_version = 'budget-operation-v2' and usage_kind != 'model')"
)
_CLAIM_V1_SHAPE = (
    "(operation_kind = 'direct' and usage_kind in ('model','embedding') "
    "and identity_schema_version = 'budget-operation-v1' and request_hash is null) or "
    "(operation_kind = 'delegation' and usage_kind = 'delegation' "
    "and identity_schema_version = 'budget-delegation-v1' and request_hash is not null)"
)
_ALLOCATION_V1_SHAPE = (
    "usage_kind in ('model','embedding') and identity_schema_version = 'budget-operation-v1'"
)
_ROUTE_CHAIN_STATE_PRESENT = "storage.route_chain_state_present"
_EMPTY_EVIDENCE_OPT_IN = "allow_empty_evidence_downgrade=true"
_SHARED_BUDGET_EVIDENCE_TABLES = (
    "parent_budget_ledgers",
    "budget_operation_claims",
    "delegation_budget_allocations",
)


def _identity_text(key: str) -> sa.ColumnElement[str]:
    """生成 SQLite/PostgreSQL 都可编译的 v1 JSON 文本字段表达式。"""

    return sa.column("identity_json", sa.JSON())[key].as_string()


def _required_identity_equal(key: str, value: object) -> sa.ColumnElement[bool]:
    field = _identity_text(key)
    return sa.and_(field.is_not(None), field == value)


def _required_identity_text(key: str) -> sa.ColumnElement[bool]:
    field = _identity_text(key)
    return sa.and_(field.is_not(None), sa.func.length(field) > 0)


def _claim_identity_json_shape_v1() -> sa.ColumnElement[bool]:
    """逐字恢复 0016 claim JSON/关系列绑定，并禁止遗留 v2 字段。"""

    operation_kind = sa.column("operation_kind", sa.String())
    usage_kind = sa.column("usage_kind", sa.String())
    schema_version = sa.column("identity_schema_version", sa.String())
    agent_id = sa.column("agent_id", sa.String())
    return sa.and_(
        _required_identity_equal("ownership_kind", operation_kind),
        _required_identity_equal("usage_kind", usage_kind),
        _required_identity_equal("identity_schema_version", schema_version),
        _required_identity_equal("identity_hash", sa.column("identity_hash", sa.String())),
        _required_identity_equal("run_id", sa.column("run_id", sa.String())),
        _required_identity_equal("agent_id", agent_id),
        _required_identity_text("request_fingerprint"),
        _required_identity_text("fingerprint_key_version"),
        _identity_text("route_chain_digest").is_(None),
        sa.column("identity_json", sa.JSON())["route_candidate_count"].as_integer().is_(None),
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
                _required_identity_equal(
                    "delegation_claim_id", sa.column("delegation_id", sa.String())
                ),
                _required_identity_equal("source_agent_id", agent_id),
                _required_identity_text("target_agent_id"),
                _required_identity_text("target_route_catalog_digest"),
            ),
        ),
    )


def _allocation_identity_json_shape_v1() -> sa.ColumnElement[bool]:
    """逐字恢复 0016 allocation JSON/关系列绑定，并禁止遗留 v2 字段。"""

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
        _identity_text("route_chain_digest").is_(None),
        sa.column("identity_json", sa.JSON())["route_candidate_count"].as_integer().is_(None),
        _required_identity_text("request_fingerprint"),
        _required_identity_text("fingerprint_key_version"),
    )


def upgrade() -> None:
    """增加 nullable state，并让数据库 identity 约束接受封闭的 v2 形状。"""

    with op.batch_alter_table("budget_operation_claims") as batch:
        batch.add_column(sa.Column("route_chain_state_json", sa.JSON(), nullable=True))
        batch.drop_constraint("ck_budget_claim_identity_shape", type_="check")
        batch.create_check_constraint("ck_budget_claim_identity_shape", _CLAIM_V2_SHAPE)
        batch.drop_constraint("ck_budget_claim_identity_json_shape", type_="check")
        batch.create_check_constraint(
            "ck_budget_claim_identity_json_shape", claim_identity_json_shape()
        )
    with op.batch_alter_table("delegation_budget_allocations") as batch:
        batch.add_column(sa.Column("route_chain_state_json", sa.JSON(), nullable=True))
        batch.drop_constraint("ck_budget_allocation_identity_shape", type_="check")
        batch.create_check_constraint("ck_budget_allocation_identity_shape", _ALLOCATION_V2_SHAPE)
        batch.drop_constraint("ck_budget_allocation_identity_json_shape", type_="check")
        batch.create_check_constraint(
            "ck_budget_allocation_identity_json_shape", allocation_identity_json_shape()
        )


def _downgrade_stops_at_0016() -> bool:
    """用 Alembic 实际 step resolver 解析相对目标，避免把 `-1` 当成 revision id。

    Alembic 1.18 的公开 context 只返回原始相对参数；这里复用 command.downgrade
    内部的同一 resolver，并由 migration 合同锁定该版本相关行为。
    """

    destination = context.get_revision_argument()
    current = context.get_context().get_current_revision()
    if not isinstance(destination, str) or not isinstance(current, str) or current != revision:
        raise RuntimeError("0017 downgrade revision context is invalid")
    script_directory = ScriptDirectory.from_config(context.config)
    steps = script_directory._downgrade_revs(  # pyright: ignore[reportPrivateUsage]
        destination,
        current,
    )
    if not steps or steps[0].from_revisions_no_deps != (revision,):
        raise RuntimeError("0017 downgrade path is invalid")
    return steps[-1].to_revisions_no_deps == (down_revision,)


def downgrade() -> None:
    """仅在两张表都没有 v2 identity/state 时恢复 v1 约束并移除列。"""

    connection = op.get_bind()
    for table_name in ("budget_operation_claims", "delegation_budget_allocations"):
        count = connection.execute(
            sa.text(
                f"select count(*) from {table_name} "
                "where route_chain_state_json is not null "
                "or identity_schema_version = 'budget-operation-v2'"
            )
        ).scalar_one()
        if count:
            raise RuntimeError(_ROUTE_CHAIN_STATE_PRESENT)

    if not _downgrade_stops_at_0016():
        # SQLite 的 batch DDL 不能保证跨多个 revision 一起回滚。目标低于 0016
        # 时必须在删除 0017 列前执行下一 revision 的证据门禁，避免失败后留下
        # schema 与版本号均已部分降级的数据库；只退到 0016 则保留 v1 evidence。
        arguments = context.get_x_argument(as_dictionary=False)
        if arguments != [_EMPTY_EVIDENCE_OPT_IN]:
            raise RuntimeError("0016 downgrade requires explicit opt-in")
        for table_name in _SHARED_BUDGET_EVIDENCE_TABLES:
            count = connection.execute(sa.text(f"select count(*) from {table_name}")).scalar_one()
            if count:
                raise RuntimeError("0016 shared budget evidence exists; downgrade refused")

    with op.batch_alter_table("delegation_budget_allocations") as batch:
        batch.drop_constraint("ck_budget_allocation_identity_json_shape", type_="check")
        batch.drop_constraint("ck_budget_allocation_identity_shape", type_="check")
        batch.create_check_constraint("ck_budget_allocation_identity_shape", _ALLOCATION_V1_SHAPE)
        batch.create_check_constraint(
            "ck_budget_allocation_identity_json_shape",
            _allocation_identity_json_shape_v1(),
        )
        batch.drop_column("route_chain_state_json")
    with op.batch_alter_table("budget_operation_claims") as batch:
        batch.drop_constraint("ck_budget_claim_identity_json_shape", type_="check")
        batch.drop_constraint("ck_budget_claim_identity_shape", type_="check")
        batch.create_check_constraint("ck_budget_claim_identity_shape", _CLAIM_V1_SHAPE)
        batch.create_check_constraint(
            "ck_budget_claim_identity_json_shape",
            _claim_identity_json_shape_v1(),
        )
        batch.drop_column("route_chain_state_json")
