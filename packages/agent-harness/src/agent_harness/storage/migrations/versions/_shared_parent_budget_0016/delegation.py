"""0016 legacy delegation reservation、aggregate 与 released proof 校验。"""

# Alembic 的 JSON checkpoint 与动态 table clause 在逐字段运行时校验后才使用；
# SQLAlchemy stubs 无法把这些 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.values import (
    _decimal,
    _integer,
)


def _released_delegation_proof_valid(
    connection: sa.Connection,
    *,
    tenant_id: object,
    root_id: object,
    delegation_id: str,
) -> bool:
    """只接受可恢复为 claimed/failed 事件对且没有 child 副作用的 released。"""

    relation = (
        connection.execute(
            sa.text(
                "select child_run_id,source_agent_id,target_agent_id,trace_id,status,error_json,"
                "event_operation_kind,event_registry_version,reserved_event_count "
                "from agent_delegations where id=:delegation_id and tenant_id=:tenant_id "
                "and parent_run_id=:root_id"
            ).columns(error_json=sa.JSON()),
            {
                "tenant_id": tenant_id,
                "root_id": root_id,
                "delegation_id": delegation_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if relation is None:
        return False
    error = relation["error_json"]
    if (
        relation["child_run_id"] is not None
        or relation["status"] != "failed"
        or not isinstance(error, Mapping)
        or error.get("code") != "delegation.execution_failed"
        or relation["event_operation_kind"] != "delegation"
        or relation["event_registry_version"] != "v1"
        or relation["reserved_event_count"] != 3
    ):
        return False

    child_count = connection.execute(
        sa.text(
            "select count(*) from agent_runs where tenant_id=:tenant_id "
            "and parent_run_id=:root_id and idempotency_key=:child_key"
        ),
        {
            "tenant_id": tenant_id,
            "root_id": root_id,
            "child_key": f"delegation:{delegation_id}",
        },
    ).scalar_one()
    aggregate_count = connection.execute(
        sa.text(
            "select count(*) from delegation_aggregates "
            "where tenant_id=:tenant_id and parent_run_id=:root_id "
            "and delegation_id=:delegation_id"
        ),
        {
            "tenant_id": tenant_id,
            "root_id": root_id,
            "delegation_id": delegation_id,
        },
    ).scalar_one()
    if child_count or aggregate_count:
        return False

    canonical_side_effects = list(
        connection.execute(
            sa.text(
                "select id,event_type,payload_json from canonical_events "
                "where tenant_id=:tenant_id and run_id=:root_id "
                "and event_type in ('delegation.child.created','delegation.completed')"
            ).columns(payload_json=sa.JSON()),
            {"tenant_id": tenant_id, "root_id": root_id},
        ).mappings()
    )
    for event in canonical_side_effects:
        payload = event["payload_json"]
        if event["id"] in {
            f"delegation:{delegation_id}:child",
            f"delegation:{delegation_id}:final",
        } or (isinstance(payload, Mapping) and payload.get("delegation_id") == delegation_id):
            return False

    group_id = f"delegation:{delegation_id}:evidence"
    group = list(
        connection.execute(
            sa.text(
                "select event_id,state,result_json,reserved_event_count,sequence_in_group,"
                "operation_kind from run_evidence_outbox "
                "where tenant_id=:tenant_id and run_id=:root_id and group_id=:group_id "
                "order by sequence_in_group"
            ).columns(result_json=sa.JSON()),
            {"tenant_id": tenant_id, "root_id": root_id, "group_id": group_id},
        ).mappings()
    )
    expected_event_ids = [
        f"delegation:{delegation_id}:claimed",
        f"delegation:{delegation_id}:child",
        f"delegation:{delegation_id}:final",
    ]
    states = tuple(row["state"] for row in group)
    if len(group) != 3 or states not in {
        ("published", "cancelled", "published"),
        ("published", "cancelled", "result_persisted"),
        ("result_persisted", "cancelled", "result_persisted"),
    }:
        return False
    for index, row in enumerate(group):
        result = row["result_json"]
        allowed_statuses = {"claimed", "failed"} if index == 0 else {"claimed"}
        if index == 2:
            allowed_statuses = {"failed"}
        if (
            row["event_id"] != expected_event_ids[index]
            or row["sequence_in_group"] != index + 1
            or row["operation_kind"] != "delegation"
            or row["reserved_event_count"] != 1
            or not isinstance(result, Mapping)
            or result.get("delegation_id") != delegation_id
            or result.get("parent_run_id") != root_id
            or result.get("child_run_id") is not None
            or result.get("source_agent_id") != relation["source_agent_id"]
            or result.get("target_agent_id") != relation["target_agent_id"]
            or result.get("trace_id") != relation["trace_id"]
            or result.get("status") not in allowed_statuses
        ):
            return False

    pending_reserved = connection.execute(
        sa.text(
            "select coalesce(sum(reserved_event_count),0) from run_evidence_outbox "
            "where tenant_id=:tenant_id and run_id=:root_id "
            "and state not in ('published','cancelled')"
        ),
        {"tenant_id": tenant_id, "root_id": root_id},
    ).scalar_one()
    capacity = (
        connection.execute(
            sa.text(
                "select outstanding_reserved_event_count from run_event_capacity "
                "where tenant_id=:tenant_id and run_id=:root_id"
            ),
            {"tenant_id": tenant_id, "root_id": root_id},
        )
        .mappings()
        .one_or_none()
    )
    return bool(
        capacity is not None and capacity["outstanding_reserved_event_count"] == pending_reserved
    )


def _delegation_reservation_matches_claim(
    connection: sa.Connection,
    *,
    tenant_id: object,
    root_id: object,
    delegation_id: str,
    claim: Mapping[str, object],
    cost_enabled: bool,
) -> bool:
    """逐字段证明 0015 reservation 四态可无损映射到 0016 top-level claim。"""

    reservation = (
        connection.execute(
            sa.text(
                "select reserved_tokens,reserved_cost_usd,settled_input_tokens,"
                "settled_output_tokens,settled_cost_usd,state "
                "from delegation_budget_reservations "
                "where tenant_id=:tenant_id and parent_run_id=:root_id "
                "and delegation_id=:delegation_id"
            ),
            {
                "tenant_id": tenant_id,
                "root_id": root_id,
                "delegation_id": delegation_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if reservation is None:
        return False
    try:
        reserved_tokens = _integer(reservation["reserved_tokens"], field="0015 reserved_tokens")
        reserved_cost = (
            None
            if reservation["reserved_cost_usd"] is None
            else _decimal(reservation["reserved_cost_usd"], field="0015 reserved_cost")
        )
    except RuntimeError:
        return False
    if (
        claim.get("reserved_tokens") != reserved_tokens
        or claim.get("reserved_cost") != reserved_cost
    ):
        return False
    state = str(reservation["state"])
    if state in {"reserved", "needs_review"}:
        return bool(
            claim.get("state") == state
            and claim.get("actual_tokens") is None
            and claim.get("actual_cost") is None
            and claim.get("token_impact") == reserved_tokens
            and claim.get("cost_impact")
            == (reserved_cost if cost_enabled and reserved_cost is not None else Decimal("0"))
        )
    if state == "released":
        return bool(
            claim.get("state") == "released"
            and claim.get("actual_tokens") is None
            and claim.get("actual_cost") is None
            and claim.get("token_impact") == 0
            and claim.get("cost_impact") == 0
            and _released_delegation_proof_valid(
                connection,
                tenant_id=tenant_id,
                root_id=root_id,
                delegation_id=delegation_id,
            )
        )
    if state != "settled":
        return False
    try:
        input_tokens = _integer(
            reservation["settled_input_tokens"], field="0015 settled_input_tokens"
        )
        output_tokens = _integer(
            reservation["settled_output_tokens"], field="0015 settled_output_tokens"
        )
        settled_cost = (
            None
            if reservation["settled_cost_usd"] is None
            else _decimal(reservation["settled_cost_usd"], field="0015 settled_cost")
        )
    except RuntimeError:
        return False
    if cost_enabled and (reserved_cost is None or settled_cost is None):
        return False
    actual_tokens = input_tokens + output_tokens
    expected_actual_cost = settled_cost if cost_enabled else None
    token_over = actual_tokens > reserved_tokens
    cost_over = bool(cost_enabled and cast(Decimal, settled_cost) > cast(Decimal, reserved_cost))
    needs_review = token_over or cost_over
    expected_state = "needs_review" if needs_review else "settled"
    expected_token_impact = max(reserved_tokens, actual_tokens) if needs_review else actual_tokens
    expected_cost_impact = (
        max(cast(Decimal, reserved_cost), cast(Decimal, settled_cost))
        if needs_review and cost_enabled
        else cast(Decimal, settled_cost)
        if cost_enabled
        else Decimal("0")
    )
    return bool(
        claim.get("state") == expected_state
        and claim.get("actual_tokens") == actual_tokens
        and claim.get("actual_cost") == expected_actual_cost
        and claim.get("token_impact") == expected_token_impact
        and claim.get("cost_impact") == expected_cost_impact
    )


def _settled_delegation_aggregate_valid(
    connection: sa.Connection,
    *,
    tenant_id: object,
    root_id: object,
    claim: Mapping[str, object],
    allocations: Sequence[Mapping[str, object]],
    cost_enabled: bool,
) -> bool:
    """把 0015 trusted aggregate 与 relation-first child allocation 逐值对账。"""

    delegation_id = claim.get("delegation_id")
    if not isinstance(delegation_id, str):
        return False
    reservation_state = connection.execute(
        sa.text(
            "select state from delegation_budget_reservations "
            "where tenant_id=:tenant_id and parent_run_id=:root_id "
            "and delegation_id=:delegation_id"
        ),
        {
            "tenant_id": tenant_id,
            "root_id": root_id,
            "delegation_id": delegation_id,
        },
    ).scalar_one_or_none()
    if reservation_state != "settled":
        return True
    aggregate = (
        connection.execute(
            sa.text(
                "select a.child_run_id,a.status,a.summary_json,a.evidence_refs_json,"
                "d.child_run_id as relation_child_run_id "
                "from delegation_aggregates a join agent_delegations d on d.id=a.delegation_id "
                "where a.tenant_id=:tenant_id and a.parent_run_id=:root_id "
                "and a.delegation_id=:delegation_id"
            ).columns(summary_json=sa.JSON(), evidence_refs_json=sa.JSON()),
            {
                "tenant_id": tenant_id,
                "root_id": root_id,
                "delegation_id": delegation_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if aggregate is None:
        return False
    summary = aggregate["summary_json"]
    evidence_refs = aggregate["evidence_refs_json"]
    related = [item for item in allocations if item.get("delegation_id") == delegation_id]
    if (
        aggregate["status"] != "complete"
        or aggregate["child_run_id"] != aggregate["relation_child_run_id"]
        or not isinstance(summary, Mapping)
        or summary.get("parent_run_id") != root_id
        or not isinstance(evidence_refs, list)
        or not related
        or any(
            item.get("run_id") != aggregate["child_run_id"]
            or item.get("state") != "settled"
            or item.get("actual_tokens") is None
            for item in related
        )
    ):
        return False
    input_tokens = summary.get("input_tokens")
    output_tokens = summary.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        return False
    actual_tokens = input_tokens + output_tokens
    allocation_tokens = sum(cast(int, item["actual_tokens"]) for item in related)
    expected_refs = {str(item.get("usage_call_id")) for item in related}
    if (
        claim.get("actual_tokens") != actual_tokens
        or allocation_tokens != actual_tokens
        or set(evidence_refs) != expected_refs
        or summary.get("budget_status")
        != ("exceeded" if claim.get("state") == "needs_review" else "within_budget")
    ):
        return False
    if not cost_enabled:
        return bool(
            summary.get("cost_usd") is None
            and claim.get("actual_cost") is None
            and all(item.get("actual_cost") is None for item in related)
        )
    try:
        summary_cost = _decimal(summary.get("cost_usd"), field="delegation aggregate cost")
        allocation_cost = sum(
            (
                _decimal(item.get("actual_cost"), field="delegation allocation actual cost")
                for item in related
            ),
            start=Decimal("0"),
        )
    except RuntimeError:
        return False
    return claim.get("actual_cost") == summary_cost and allocation_cost == summary_cost


__all__ = [
    "_released_delegation_proof_valid",
    "_delegation_reservation_matches_claim",
    "_settled_delegation_aggregate_valid",
]
