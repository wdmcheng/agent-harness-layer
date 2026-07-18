"""0016 legacy preflight 与 verified backfill 写入。"""

# ruff: noqa: E402

# Alembic 的 JSON checkpoint 与动态 table clause 在逐字段运行时校验后才使用；
# SQLAlchemy stubs 无法把这些 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.legacy import (
    _require_legacy_closed,
)
from agent_harness.storage.migrations.versions._shared_parent_budget_0016.plan import (
    _load_backfill_plan,
)


def _legacy_preflight(connection: sa.Connection) -> list[dict[str, Any]]:
    """DDL 前整批分类 legacy tree，并返回只含可验证事实的 backfill plan。"""

    plans: list[dict[str, Any]] = []
    roots = connection.execute(
        sa.text(
            "select id, tenant_id, agent_id, status from agent_runs "
            "where parent_run_id is null order by tenant_id, id"
        )
    ).mappings()
    for raw_root in roots:
        root: dict[str, object] = {str(key): value for key, value in raw_root.items()}
        children = list(
            connection.execute(
                sa.text(
                    "select id, agent_id, status from agent_runs "
                    "where tenant_id=:tenant_id and parent_run_id=:root_id"
                ),
                {"tenant_id": root["tenant_id"], "root_id": root["id"]},
            ).mappings()
        )
        typed_children: list[dict[str, object]] = [
            {str(key): value for key, value in child.items()} for child in children
        ]
        try:
            _require_legacy_closed(connection, root=root, children=typed_children)
        except RuntimeError as closure_error:
            plan = _load_backfill_plan(connection, root=root, children=typed_children)
            if plan is None:
                raise closure_error
            plans.append(plan)
    return plans


def _apply_backfill(connection: sa.Connection, plans: list[dict[str, Any]]) -> None:
    """所有 bundle 已在 DDL 前验证；这里只做确定性 INSERT，不再读取 current config。"""

    if not plans:
        return
    ledger_table = sa.table(
        "parent_budget_ledgers",
        sa.column("tenant_id", sa.String()),
        sa.column("budget_owner_run_id", sa.String()),
        sa.column("token_limit", sa.Integer()),
        sa.column("cost_limit", sa.Numeric(20, 8)),
        sa.column("cost_enabled", sa.Boolean()),
        sa.column("token_impact", sa.Integer()),
        sa.column("cost_impact", sa.Numeric(20, 8)),
        sa.column("state", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("registry_version", sa.String()),
        sa.column("config_version", sa.String()),
        sa.column("catalog_version", sa.String()),
        sa.column("snapshot_id", sa.String()),
        sa.column("snapshot_hash", sa.String()),
        sa.column("snapshot_json", sa.JSON()),
    )
    claim_table = sa.table(
        "budget_operation_claims",
        *[
            sa.column(name, type_)  # pyright: ignore[reportArgumentType]
            for name, type_ in (
                ("id", sa.String()),
                ("tenant_id", sa.String()),
                ("budget_owner_run_id", sa.String()),
                ("operation_kind", sa.String()),
                ("usage_call_id", sa.String()),
                ("delegation_id", sa.String()),
                ("run_id", sa.String()),
                ("agent_id", sa.String()),
                ("usage_kind", sa.String()),
                ("identity_schema_version", sa.String()),
                ("identity_hash", sa.String()),
                ("identity_json", sa.JSON()),
                ("request_hash", sa.String()),
                ("reserved_tokens", sa.Integer()),
                ("reserved_cost", sa.Numeric(20, 8)),
                ("actual_tokens", sa.Integer()),
                ("actual_cost", sa.Numeric(20, 8)),
                ("token_impact", sa.Integer()),
                ("cost_impact", sa.Numeric(20, 8)),
                ("state", sa.String()),
                ("side_effect_state", sa.String()),
                ("result_json", sa.JSON()),
                ("backfill_source", sa.String()),
            )
        ],
    )
    allocation_table = sa.table(
        "delegation_budget_allocations",
        *[
            sa.column(name, type_)  # pyright: ignore[reportArgumentType]
            for name, type_ in (
                ("id", sa.String()),
                ("tenant_id", sa.String()),
                ("budget_owner_run_id", sa.String()),
                ("delegation_id", sa.String()),
                ("usage_call_id", sa.String()),
                ("run_id", sa.String()),
                ("agent_id", sa.String()),
                ("usage_kind", sa.String()),
                ("identity_schema_version", sa.String()),
                ("identity_hash", sa.String()),
                ("identity_json", sa.JSON()),
                ("reserved_tokens", sa.Integer()),
                ("reserved_cost", sa.Numeric(20, 8)),
                ("actual_tokens", sa.Integer()),
                ("actual_cost", sa.Numeric(20, 8)),
                ("token_impact", sa.Integer()),
                ("cost_impact", sa.Numeric(20, 8)),
                ("state", sa.String()),
                ("side_effect_state", sa.String()),
                ("result_json", sa.JSON()),
                ("backfill_source", sa.String()),
            )
        ],
    )
    for plan in plans:
        ledger = plan["ledger"]
        connection.execute(
            sa.insert(ledger_table),
            {
                key: ledger[key]
                for key in (
                    "tenant_id",
                    "budget_owner_run_id",
                    "token_limit",
                    "cost_limit",
                    "cost_enabled",
                    "token_impact",
                    "cost_impact",
                    "state",
                    "version",
                    "registry_version",
                    "config_version",
                    "catalog_version",
                    "snapshot_id",
                    "snapshot_hash",
                )
            }
            | {"snapshot_json": ledger["snapshot"]},
        )
        for raw_claim in plan["claims"]:
            claim = dict(raw_claim)
            identity = claim.get("identity_json")
            claim.update(
                tenant_id=ledger["tenant_id"],
                budget_owner_run_id=ledger["budget_owner_run_id"],
                identity_schema_version=(
                    None
                    if not isinstance(identity, Mapping)
                    else identity["identity_schema_version"]
                ),
                identity_hash=(
                    None if not isinstance(identity, Mapping) else identity["identity_hash"]
                ),
                backfill_source=claim.get("backfill_source", "legacy_checkpoint_v1"),
            )
            connection.execute(sa.insert(claim_table), claim)
        for raw_allocation in plan["allocations"]:
            allocation = dict(raw_allocation)
            identity = cast(Mapping[str, object], allocation["identity_json"])
            allocation.update(
                tenant_id=ledger["tenant_id"],
                budget_owner_run_id=ledger["budget_owner_run_id"],
                identity_schema_version=identity["identity_schema_version"],
                identity_hash=identity["identity_hash"],
                backfill_source=allocation.get("backfill_source", "legacy_checkpoint_v1"),
            )
            connection.execute(sa.insert(allocation_table), allocation)


__all__ = [
    "_legacy_preflight",
    "_apply_backfill",
]
