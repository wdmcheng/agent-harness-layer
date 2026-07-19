"""0016 shared-budget 全树 backfill plan 解析与一致性验证。"""

# Alembic 的 JSON checkpoint 与动态 table clause 在逐字段运行时校验后才使用；
# SQLAlchemy stubs 无法把这些 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.catalog import (
    _snapshot_catalog_valid,
)
from agent_harness.storage.migrations.versions._shared_parent_budget_0016.operations import (
    _normalize_operations,
)
from agent_harness.storage.migrations.versions._shared_parent_budget_0016.source import (
    _load_source_baseline,
    _versioned_history_matches,
)
from agent_harness.storage.migrations.versions._shared_parent_budget_0016.values import (
    _canonical_hash,
    _decimal,
    _integer,
)


def _load_backfill_plan(
    connection: sa.Connection,
    *,
    root: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
) -> dict[str, Any] | None:
    """只接受 checkpoint 已持久化且自校验完整的 v1 backfill bundle。"""

    bundle_record = (
        connection.execute(
            sa.text(
                "select id,sequence,state_json from checkpoints "
                "where tenant_id=:tenant_id and run_id=:run_id "
                "order by sequence desc limit 1"
            ).columns(state_json=sa.JSON()),
            {"tenant_id": root["tenant_id"], "run_id": root["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if bundle_record is None:
        return None
    typed_bundle_record: dict[str, object] = {
        str(key): value for key, value in bundle_record.items()
    }
    state = bundle_record["state_json"]
    if not isinstance(state, Mapping):
        return None
    raw = state.get("shared_budget_backfill_v1")
    if not isinstance(raw, Mapping):
        return None
    ledger = raw.get("ledger")
    claims = raw.get("claims")
    allocations = raw.get("allocations")
    if (
        not isinstance(ledger, Mapping)
        or not isinstance(claims, list)
        or not isinstance(allocations, list)
    ):
        raise RuntimeError("0016 backfill bundle shape is invalid")
    source_history, delegation_fingerprint_proofs, fingerprint_proofs_hash = _load_source_baseline(
        connection,
        tenant_id=root["tenant_id"],
        run_id=root["id"],
        bundle_record=typed_bundle_record,
        bundle=raw,
    )
    snapshot = ledger.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise RuntimeError("0016 backfill snapshot is missing")
    snapshot_id = ledger.get("snapshot_id")
    if (
        not isinstance(snapshot_id, str)
        or ledger.get("snapshot_hash") != _canonical_hash(snapshot)
        or not isinstance(snapshot.get("owner"), Mapping)
        or cast(Mapping[str, object], snapshot["owner"]).get("root_run_id") != root["id"]
    ):
        raise RuntimeError("0016 backfill snapshot hash or owner is invalid")
    agents = snapshot.get("agents")
    delegation_rows = list(
        connection.execute(
            sa.text(
                "select source_agent_id,target_agent_id from agent_delegations "
                "where tenant_id=:tenant_id and parent_run_id=:root_id"
            ),
            {"tenant_id": root["tenant_id"], "root_id": root["id"]},
        )
    )
    if any(str(row.source_agent_id) != str(root["agent_id"]) for row in delegation_rows):
        raise RuntimeError("0016 backfill delegation source conflicts with owner")
    delegation_targets = {str(row.target_agent_id) for row in delegation_rows}
    delegation_agents = {
        str(agent_id)
        for row in delegation_rows
        for agent_id in (row.source_agent_id, row.target_agent_id)
    }
    owner_snapshot = cast(Mapping[str, object], snapshot["owner"])
    owner_targets = owner_snapshot.get("delegation_targets")
    if (
        not isinstance(owner_targets, list)
        or any(not isinstance(item, str) or not item for item in owner_targets)
        or len(set(owner_targets)) != len(owner_targets)
    ):
        raise RuntimeError("0016 backfill owner limits conflict with snapshot")
    frozen_targets = set(cast(list[str], owner_targets))
    tree_agents = {
        str(root["agent_id"]),
        *(str(child["agent_id"]) for child in children),
        *delegation_agents,
        *frozen_targets,
    }
    if not isinstance(agents, Mapping) or not tree_agents <= set(agents):
        raise RuntimeError("0016 backfill target sub-snapshot is incomplete")
    token_limit = _integer(ledger.get("token_limit"), field="token_limit")
    version = _integer(ledger.get("version"), field="version")
    for version_field in ("registry_version", "config_version", "catalog_version"):
        if (
            not isinstance(ledger.get(version_field), str)
            or not ledger[version_field]
            or snapshot.get(version_field) != ledger[version_field]
        ):
            raise RuntimeError(f"0016 backfill {version_field} is invalid")
    cost_enabled = ledger.get("cost_enabled")
    cost_limit_raw = ledger.get("cost_limit")
    if not isinstance(cost_enabled, bool) or cost_enabled != (cost_limit_raw is not None):
        raise RuntimeError("0016 backfill cost mode is invalid")
    cost_limit = None if cost_limit_raw is None else _decimal(cost_limit_raw, field="cost_limit")
    if (
        owner_snapshot.get("agent_id") != root["agent_id"]
        or not delegation_targets <= frozen_targets
        or any(
            not isinstance(item, str) or item not in cast(Mapping[object, object], agents)
            for item in owner_targets
        )
        or owner_snapshot.get("max_tokens_per_run") != token_limit
        or owner_snapshot.get("cost_enabled") != cost_enabled
        or (
            None
            if owner_snapshot.get("max_cost_usd_per_run") is None
            else _decimal(owner_snapshot.get("max_cost_usd_per_run"), field="owner cost limit")
        )
        != cost_limit
    ):
        raise RuntimeError("0016 backfill owner limits conflict with snapshot")
    if not _snapshot_catalog_valid(
        snapshot,
        tree_agents=tree_agents,
        token_limit=token_limit,
        cost_enabled=cost_enabled,
        cost_limit=cost_limit,
    ):
        raise RuntimeError("0016 backfill target sub-snapshot is incomplete")
    if not _versioned_history_matches(
        source_history,
        ledger=ledger,
        snapshot=snapshot,
        delegation_fingerprint_proofs_hash=fingerprint_proofs_hash,
    ):
        raise RuntimeError("0016 backfill versioned history is invalid")
    normalized_claims, normalized_allocations = _normalize_operations(
        connection,
        root=root,
        children=children,
        claims=claims,
        allocations=allocations,
        snapshot_id=snapshot_id,
        snapshot=snapshot,
        cost_enabled=cost_enabled,
        delegation_fingerprint_proofs=delegation_fingerprint_proofs,
    )
    token_impact = _integer(ledger.get("token_impact"), field="token_impact")
    cost_impact = _decimal(ledger.get("cost_impact"), field="cost_impact")
    claim_token_sum = sum(
        _integer(item.get("token_impact"), field="claim token impact") for item in normalized_claims
    )
    claim_cost_sum = sum(
        (
            _decimal(item.get("cost_impact"), field="claim cost impact")
            for item in normalized_claims
        ),
        start=Decimal("0"),
    )
    state_value = ledger.get("state")
    has_needs_review = any(
        item.get("state") == "needs_review"
        for item in [*normalized_claims, *normalized_allocations]
    )
    if (
        token_impact != claim_token_sum
        or cost_impact != claim_cost_sum
        or state_value not in {"active", "needs_review"}
        or (has_needs_review and state_value != "needs_review")
        or (token_impact > token_limit and state_value != "needs_review")
        or (
            cost_enabled
            and cast(Decimal, cost_limit) < cost_impact
            and state_value != "needs_review"
        )
        or (not cost_enabled and cost_impact != 0)
    ):
        raise RuntimeError("0016 backfill aggregate is inconsistent")
    return {
        "ledger": {
            **dict(ledger),
            "tenant_id": root["tenant_id"],
            "budget_owner_run_id": root["id"],
            "cost_limit": cost_limit,
            "cost_impact": cost_impact,
            "version": version,
        },
        "claims": normalized_claims,
        "allocations": normalized_allocations,
    }


__all__ = ["_load_backfill_plan"]
