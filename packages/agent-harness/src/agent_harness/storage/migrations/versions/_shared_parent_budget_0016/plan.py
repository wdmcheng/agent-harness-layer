"""0016 shared-budget 全树 backfill plan 解析与一致性验证。"""

# ruff: noqa: E402

# Alembic 的 JSON checkpoint 与动态 table clause 在逐字段运行时校验后才使用；
# SQLAlchemy stubs 无法把这些 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import sqlalchemy as sa


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise RuntimeError(f"0016 backfill {field} is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"0016 backfill {field} is invalid") from exc
    if not result.is_finite() or result < 0:
        raise RuntimeError(f"0016 backfill {field} is invalid")
    return result


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"0016 backfill {field} is invalid")
    return value


from agent_harness.storage.migrations.versions._shared_parent_budget_0016.delegation import (
    _delegation_reservation_matches_claim,
    _settled_delegation_aggregate_valid,
)
from agent_harness.storage.migrations.versions._shared_parent_budget_0016.legacy import (
    _identity_valid,
    _normalize_detail_values,
    _snapshot_catalog_valid,
    _usage_link_valid,
)


def _load_backfill_plan(
    connection: sa.Connection,
    *,
    root: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
) -> dict[str, Any] | None:
    """只接受 checkpoint 已持久化且自校验完整的 v1 backfill bundle。"""

    state = connection.execute(
        sa.text(
            "select state_json from checkpoints where tenant_id=:tenant_id and run_id=:run_id "
            "order by sequence desc limit 1"
        ).columns(state_json=sa.JSON()),
        {"tenant_id": root["tenant_id"], "run_id": root["id"]},
    ).scalar_one_or_none()
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
    normalized_claims: list[dict[str, Any]] = []
    normalized_allocations: list[dict[str, Any]] = []
    child_by_id = {str(child["id"]): child for child in children}
    for raw_claim in claims:
        if not isinstance(raw_claim, Mapping):
            raise RuntimeError("0016 backfill claim is invalid")
        claim = _normalize_detail_values(raw_claim, allocation=False)
        kind = claim.get("operation_kind")
        run_id = claim.get("run_id")
        agent_id = claim.get("agent_id")
        if not isinstance(run_id, str) or not isinstance(agent_id, str):
            raise RuntimeError("0016 backfill claim scope is invalid")
        if kind == "direct":
            identity = claim.get("identity_json")
            usage_call_id = claim.get("usage_call_id")
            if (
                run_id != root["id"]
                or not isinstance(usage_call_id, str)
                or not isinstance(identity, Mapping)
                or not _identity_valid(
                    identity,
                    detail=claim,
                    ownership_kind="direct",
                    run_id=run_id,
                    agent_id=agent_id,
                    delegation_id=None,
                    snapshot_id=snapshot_id,
                    snapshot=snapshot,
                    cost_enabled=cost_enabled,
                )
                or not _usage_link_valid(
                    connection,
                    tenant_id=root["tenant_id"],
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                    usage_kind=claim.get("usage_kind"),
                    side_effect_state=claim.get("side_effect_state"),
                    result=claim.get("result_json"),
                )
            ):
                raise RuntimeError("0016 backfill direct identity is invalid")
        elif kind == "delegation":
            delegation_id = claim.get("delegation_id")
            relation = connection.execute(
                sa.text(
                    "select request_hash from agent_delegations "
                    "where id=:delegation_id and tenant_id=:tenant_id and parent_run_id=:root_id"
                ),
                {
                    "delegation_id": delegation_id,
                    "tenant_id": root["tenant_id"],
                    "root_id": root["id"],
                },
            ).scalar_one_or_none()
            if (
                not isinstance(delegation_id, str)
                or relation != claim.get("request_hash")
                or not _delegation_reservation_matches_claim(
                    connection,
                    tenant_id=root["tenant_id"],
                    root_id=root["id"],
                    delegation_id=delegation_id,
                    claim=claim,
                    cost_enabled=cost_enabled,
                )
            ):
                raise RuntimeError("0016 backfill delegation linkage is invalid")
        else:
            raise RuntimeError("0016 backfill operation kind is invalid")
        normalized_claims.append(claim)
    for raw_allocation in allocations:
        if not isinstance(raw_allocation, Mapping):
            raise RuntimeError("0016 backfill allocation is invalid")
        allocation = _normalize_detail_values(raw_allocation, allocation=True)
        run_id = allocation.get("run_id")
        agent_id = allocation.get("agent_id")
        delegation_id = allocation.get("delegation_id")
        identity = allocation.get("identity_json")
        relation = connection.execute(
            sa.text(
                "select count(*) from agent_delegations where id=:delegation_id "
                "and tenant_id=:tenant_id and parent_run_id=:root_id and child_run_id=:child_id"
            ),
            {
                "delegation_id": delegation_id,
                "tenant_id": root["tenant_id"],
                "root_id": root["id"],
                "child_id": run_id,
            },
        ).scalar_one()
        if (
            not isinstance(run_id, str)
            or run_id not in child_by_id
            or child_by_id[run_id]["agent_id"] != agent_id
            or not isinstance(delegation_id, str)
            or relation != 1
            or not isinstance(identity, Mapping)
            or not _identity_valid(
                identity,
                detail=allocation,
                ownership_kind="allocation",
                run_id=run_id,
                agent_id=str(agent_id),
                delegation_id=delegation_id,
                snapshot_id=snapshot_id,
                snapshot=snapshot,
                cost_enabled=cost_enabled,
            )
            or not _usage_link_valid(
                connection,
                tenant_id=root["tenant_id"],
                run_id=run_id,
                usage_call_id=allocation.get("usage_call_id"),
                usage_kind=allocation.get("usage_kind"),
                side_effect_state=allocation.get("side_effect_state"),
                result=allocation.get("result_json"),
            )
        ):
            raise RuntimeError("0016 backfill allocation identity is invalid")
        normalized_allocations.append(allocation)
    if any(
        item.get("operation_kind") == "delegation"
        and not _settled_delegation_aggregate_valid(
            connection,
            tenant_id=root["tenant_id"],
            root_id=root["id"],
            claim=item,
            allocations=normalized_allocations,
            cost_enabled=cost_enabled,
        )
        for item in normalized_claims
    ):
        raise RuntimeError("0016 backfill delegation aggregate is invalid")
    expected_direct = {
        str(row.usage_call_id)
        for row in connection.execute(
            sa.text(
                "select usage_call_id from run_evidence_outbox "
                "where tenant_id=:tenant_id and run_id=:root_id "
                "and operation_kind in ('model_usage','embedding_usage')"
            ),
            {"tenant_id": root["tenant_id"], "root_id": root["id"]},
        )
    }
    delegation_rows = list(
        connection.execute(
            sa.text(
                "select d.id, r.id as reservation_id from agent_delegations d "
                "left join delegation_budget_reservations r "
                "on r.delegation_id=d.id and r.tenant_id=d.tenant_id "
                "and r.parent_run_id=d.parent_run_id "
                "where d.tenant_id=:tenant_id and d.parent_run_id=:root_id"
            ),
            {"tenant_id": root["tenant_id"], "root_id": root["id"]},
        )
    )
    delegation_ids = [str(row.id) for row in delegation_rows]
    if any(row.reservation_id is None for row in delegation_rows) or len(delegation_ids) != len(
        set(delegation_ids)
    ):
        # checkpoint 只能恢复完整的 0015 delegation/reservation 一一对应关系；缺失或重复时
        # 不允许用空 claims 绕过迁移前置检查。
        raise RuntimeError("0016 backfill delegation reservation is missing or ambiguous")
    expected_delegations = set(delegation_ids)
    expected_allocations = {
        (str(row.delegation_id), str(row.usage_call_id))
        for row in connection.execute(
            sa.text(
                "select d.id as delegation_id, o.usage_call_id as usage_call_id "
                "from agent_delegations d "
                "join run_evidence_outbox o "
                "on o.tenant_id=d.tenant_id and o.run_id=d.child_run_id "
                "where d.tenant_id=:tenant_id and d.parent_run_id=:root_id "
                "and o.operation_kind in ('model_usage','embedding_usage')"
            ),
            {"tenant_id": root["tenant_id"], "root_id": root["id"]},
        )
    }
    provided_direct = {
        str(item["usage_call_id"])
        for item in normalized_claims
        if item.get("operation_kind") == "direct"
    }
    provided_delegations = {
        str(item["delegation_id"])
        for item in normalized_claims
        if item.get("operation_kind") == "delegation"
    }
    provided_allocations = {
        (str(item["delegation_id"]), str(item["usage_call_id"])) for item in normalized_allocations
    }
    if (
        provided_direct != expected_direct
        or provided_delegations != expected_delegations
        or provided_allocations != expected_allocations
    ):
        raise RuntimeError("0016 backfill bundle omits or invents durable operation evidence")
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


__all__ = [
    "_load_backfill_plan",
]
