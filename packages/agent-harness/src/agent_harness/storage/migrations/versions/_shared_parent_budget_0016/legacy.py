"""0016 legacy closure、identity 与 frozen snapshot 校验。"""

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
    _released_delegation_proof_valid,
)


def _require_legacy_closed(
    connection: sa.Connection,
    *,
    root: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
) -> None:
    """逐树证明 legacy_closed；调用方可在失败后尝试完整 snapshot backfill。"""

    tree_ids = [str(root["id"]), *(str(value["id"]) for value in children)]
    if root["status"] not in {"completed", "failed", "cancelled"} or any(
        child["status"] not in {"completed", "failed", "cancelled"} for child in children
    ):
        raise RuntimeError("0016 legacy tree is active without immutable budget snapshot")
    terminal_types = {
        "completed": "run.completed",
        "failed": "run.failed",
        "cancelled": "run.cancelled",
    }
    if connection.dialect.name == "postgresql":
        for run_id, status in [
            (root["id"], root["status"]),
            *((child["id"], child["status"]) for child in children),
        ]:
            terminal_rows = connection.execute(
                sa.text(
                    "select event_type from canonical_events "
                    "where tenant_id=:tenant_id and run_id=:run_id and terminal=true"
                ),
                {"tenant_id": root["tenant_id"], "run_id": run_id},
            ).scalars()
            if list(terminal_rows) != [terminal_types[str(status)]]:
                raise RuntimeError("0016 legacy tree lacks PostgreSQL terminal closure proof")
    for run_id in tree_ids:
        pending_queue = connection.execute(
            sa.text(
                "select count(*) from agent_runs where tenant_id=:tenant_id and id=:run_id "
                "and (queue_operation_id is not null "
                "or queue_request_id is not null "
                "or queue_effective_idempotency_key is not null "
                "or queue_enqueue_state is not null "
                "or queue_message_id is not null "
                "or execution_owner_id is not null "
                "or execution_workflow_id is not null)"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_queue:
            raise RuntimeError("0016 legacy tree has pending queue recovery")
        capacity = (
            connection.execute(
                sa.text(
                    "select outstanding_reserved_event_count, terminal_reservation "
                    "from run_event_capacity where tenant_id=:tenant_id and run_id=:run_id"
                ),
                {"tenant_id": root["tenant_id"], "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            capacity is None
            or capacity["outstanding_reserved_event_count"] != 0
            or capacity["terminal_reservation"] != 0
        ):
            raise RuntimeError("0016 legacy tree lacks terminal capacity closure proof")
        pending_outbox = connection.execute(
            sa.text(
                "select count(*) from run_evidence_outbox "
                "where tenant_id=:tenant_id and run_id=:run_id "
                "and state not in ('published','cancelled')"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_outbox:
            raise RuntimeError("0016 legacy tree has pending evidence outbox")
        pending_approval = connection.execute(
            sa.text(
                "select count(*) from approvals where tenant_id=:tenant_id and run_id=:run_id "
                "and (status not in ('approved','denied') "
                "or resolution_state is null "
                "or resolution_state not in ('completed','failed') "
                "or resolution_operation_id is not null "
                "or resolution_enqueue_state is not null "
                "or resolution_message_id is not null "
                "or resolution_workflow_owner_id is not null "
                "or resolution_workflow_id is not null)"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_approval:
            raise RuntimeError("0016 legacy tree has pending approval recovery")
        pending_tool = connection.execute(
            sa.text(
                "select count(*) from tool_invocations "
                "where tenant_id=:tenant_id and run_id=:run_id and execution_state='executing'"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_tool:
            raise RuntimeError("0016 legacy tree has pending tool recovery")
    pending_delegation = connection.execute(
        sa.text(
            "select count(*) from agent_delegations d "
            "left join delegation_budget_reservations r "
            "on r.delegation_id=d.id and r.tenant_id=d.tenant_id "
            "where d.tenant_id=:tenant_id and d.parent_run_id=:root_id "
            "and (r.id is null "
            "or d.status not in ('completed','failed','released') "
            "or r.state not in ('settled','released'))"
        ),
        {"tenant_id": root["tenant_id"], "root_id": root["id"]},
    ).scalar_one()
    if pending_delegation:
        raise RuntimeError("0016 legacy tree has pending delegation evidence")

    released_delegation_ids = connection.execute(
        sa.text(
            "select d.id from agent_delegations d "
            "join delegation_budget_reservations r on r.delegation_id=d.id "
            "where d.tenant_id=:tenant_id and d.parent_run_id=:root_id "
            "and r.state='released'"
        ),
        {"tenant_id": root["tenant_id"], "root_id": root["id"]},
    ).scalars()
    for delegation_id in released_delegation_ids:
        if not _released_delegation_proof_valid(
            connection,
            tenant_id=root["tenant_id"],
            root_id=root["id"],
            delegation_id=str(delegation_id),
        ):
            raise RuntimeError("0016 legacy tree released delegation proof is invalid")


def _identity_valid(
    identity: Mapping[str, object],
    *,
    detail: Mapping[str, object],
    ownership_kind: str,
    run_id: str,
    agent_id: str,
    delegation_id: str | None,
    snapshot_id: str,
    snapshot: Mapping[str, object],
    cost_enabled: bool,
) -> bool:
    identity_hash = identity.get("identity_hash")
    payload = dict(identity)
    payload.pop("identity_hash", None)
    for optional_field in (
        "delegation_claim_id",
        "price_source_ref",
        "price_source_version",
        "cache_key_digest",
        "trusted_cost_bound",
    ):
        payload.setdefault(optional_field, None)
    usage_kind = detail.get("usage_kind")
    if usage_kind not in {"model", "embedding"}:
        return False
    try:
        trusted_tokens = _integer(
            identity.get("trusted_token_bound"), field="identity trusted token bound"
        )
        trusted_cost = (
            None
            if identity.get("trusted_cost_bound") is None
            else _decimal(identity.get("trusted_cost_bound"), field="identity trusted cost bound")
        )
    except RuntimeError:
        return False
    reserved_tokens = detail.get("reserved_tokens")
    reserved_cost = detail.get("reserved_cost")
    result = detail.get("result_json")
    decision = (
        result.get("evidence", {}).get("decision", {})
        if isinstance(result, Mapping)
        and isinstance(result.get("evidence"), Mapping)
        and isinstance(cast(Mapping[str, object], result["evidence"]).get("decision"), Mapping)
        else {}
    )
    cache_hit = bool(
        detail.get("state") == "settled"
        and detail.get("side_effect_state") == "result_committed"
        and detail.get("token_impact") == 0
        and detail.get("cost_impact") == 0
        and cast(Mapping[str, object], decision).get("provider_called") is False
        and cast(Mapping[str, object], decision).get("cache_status") == "hit"
    )
    if (
        identity.get("usage_kind") != usage_kind
        or identity.get("cost_enabled") is not cost_enabled
        or cost_enabled != (trusted_cost is not None)
        or (not cost_enabled and reserved_cost is not None)
        or (
            reserved_tokens is not None
            and trusted_tokens != reserved_tokens
            and not (cache_hit and reserved_tokens == 0)
        )
        or (
            reserved_cost is not None
            and trusted_cost != reserved_cost
            and not (cache_hit and reserved_cost == 0)
        )
        or (ownership_kind == "direct" and reserved_tokens is None)
        or (ownership_kind == "direct" and cost_enabled and reserved_cost is None)
        or (usage_kind == "model" and identity.get("cache_key_digest") is not None)
        or (
            usage_kind == "embedding"
            and (
                not isinstance(identity.get("cache_key_digest"), str)
                or not identity.get("cache_key_digest")
            )
        )
    ):
        return False
    agents = snapshot.get("agents")
    agent_snapshot = agents.get(agent_id) if isinstance(agents, Mapping) else None
    routes = agent_snapshot.get("routes") if isinstance(agent_snapshot, Mapping) else None
    route_matches = isinstance(routes, list) and any(
        isinstance(route, Mapping)
        and route.get("usage_kind") == usage_kind
        and route.get("provider") == identity.get("provider")
        and route.get("model") == identity.get("model")
        and route.get("price_source_ref") == identity.get("price_source_ref")
        and route.get("price_source_version") == identity.get("price_source_version")
        for route in routes
    )
    target_budget = (
        agent_snapshot.get("target_budget") if isinstance(agent_snapshot, Mapping) else None
    )
    if not isinstance(target_budget, Mapping):
        return False
    try:
        target_tokens = _integer(
            target_budget.get("max_tokens_per_run"), field="target token limit"
        )
        target_cost = (
            None
            if target_budget.get("max_cost_usd_per_run") is None
            else _decimal(target_budget.get("max_cost_usd_per_run"), field="target cost limit")
        )
    except RuntimeError:
        return False
    owner = snapshot.get("owner")
    if not isinstance(owner, Mapping):
        return False
    try:
        owner_cost = (
            None
            if owner.get("max_cost_usd_per_run") is None
            else _decimal(owner.get("max_cost_usd_per_run"), field="owner cost limit")
        )
    except RuntimeError:
        return False
    effective_target_cost = target_cost if target_cost is not None else owner_cost
    return bool(
        isinstance(identity_hash, str)
        and identity_hash == _canonical_hash(payload)
        and identity.get("identity_schema_version") == "budget-operation-v1"
        and identity.get("ownership_kind") == ownership_kind
        and identity.get("run_id") == run_id
        and identity.get("agent_id") == agent_id
        and identity.get("delegation_claim_id") == delegation_id
        and identity.get("tree_snapshot_id") == snapshot_id
        and identity.get("agent_sub_snapshot_id") == f"{snapshot_id}:{agent_id}"
        and trusted_tokens <= target_tokens
        and (
            not cost_enabled or cast(Decimal, trusted_cost) <= cast(Decimal, effective_target_cost)
        )
        and isinstance(identity.get("operation_slot"), str)
        and bool(identity.get("operation_slot"))
        and isinstance(identity.get("request_fingerprint"), str)
        and bool(identity.get("request_fingerprint"))
        and isinstance(identity.get("fingerprint_key_version"), str)
        and bool(identity.get("fingerprint_key_version"))
        and route_matches
    )


def _usage_link_valid(
    connection: sa.Connection,
    *,
    tenant_id: object,
    run_id: str,
    usage_call_id: object,
    usage_kind: object,
    side_effect_state: object,
    result: object,
) -> bool:
    if not isinstance(usage_call_id, str) or usage_kind not in {"model", "embedding"}:
        return False
    rows = list(
        connection.execute(
            sa.text(
                "select operation_kind, state, result_json from run_evidence_outbox "
                "where tenant_id=:tenant_id and run_id=:run_id and usage_call_id=:usage_call_id"
            ).columns(result_json=sa.JSON()),
            {"tenant_id": tenant_id, "run_id": run_id, "usage_call_id": usage_call_id},
        ).mappings()
    )
    if len(rows) != 1 or rows[0]["operation_kind"] != f"{usage_kind}_usage":
        return False
    if side_effect_state == "result_committed":
        return (
            rows[0]["state"] in {"result_persisted", "published"}
            and rows[0]["result_json"] == result
        )
    return side_effect_state in {"not_started", "started"} and rows[0]["state"] == "started"


def _snapshot_catalog_valid(
    snapshot: Mapping[str, object],
    *,
    tree_agents: set[str],
    token_limit: int,
    cost_enabled: bool,
    cost_limit: Decimal | None,
) -> bool:
    """验证 bundle 自带的 source/target descriptor、budget、route 与 price catalog。"""

    raw_agents = snapshot.get("agents")
    if not isinstance(raw_agents, Mapping):
        return False
    agents = cast(Mapping[object, object], raw_agents)
    for agent_id in tree_agents:
        raw_sub_snapshot = agents.get(agent_id)
        if not isinstance(raw_sub_snapshot, Mapping):
            return False
        sub_snapshot = cast(Mapping[str, object], raw_sub_snapshot)
        policy = sub_snapshot.get("model_policy")
        target_budget = sub_snapshot.get("target_budget")
        routes = sub_snapshot.get("routes")
        if (
            sub_snapshot.get("agent_id") != agent_id
            or not isinstance(sub_snapshot.get("descriptor_version"), str)
            or not sub_snapshot.get("descriptor_version")
            or not isinstance(policy, Mapping)
            or not isinstance(target_budget, Mapping)
            or not isinstance(routes, list)
            or not routes
        ):
            return False
        typed_policy = cast(Mapping[str, object], policy)
        raw_fallbacks = typed_policy.get("fallback_models")
        if (
            not isinstance(typed_policy.get("provider"), str)
            or not typed_policy.get("provider")
            or not isinstance(typed_policy.get("default_model"), str)
            or not typed_policy.get("default_model")
            or not isinstance(raw_fallbacks, list)
        ):
            return False
        provider = cast(str, typed_policy["provider"])
        default_model = cast(str, typed_policy["default_model"])
        fallback_values = cast(list[object], raw_fallbacks)
        if not all(isinstance(item, str) and item for item in fallback_values):
            return False
        policy_models = [default_model, *cast(list[str], fallback_values)]
        if len(set(policy_models)) != len(policy_models):
            return False
        allowed_model_routes = {(provider, model) for model in policy_models}
        typed_budget = cast(Mapping[str, object], target_budget)
        target_tokens = typed_budget.get("max_tokens_per_run")
        if (
            isinstance(target_tokens, bool)
            or not isinstance(target_tokens, int)
            or target_tokens < 0
            or target_tokens > token_limit
        ):
            return False
        raw_target_cost = typed_budget.get("max_cost_usd_per_run")
        try:
            target_cost = (
                None
                if raw_target_cost is None
                else _decimal(raw_target_cost, field="target cost limit")
            )
        except RuntimeError:
            return False
        if (not cost_enabled and target_cost is not None) or (
            cost_enabled and target_cost is not None and target_cost > cast(Decimal, cost_limit)
        ):
            return False
        frozen_model_routes: list[tuple[str, str]] = []
        for raw_route in routes:
            if not isinstance(raw_route, Mapping):
                return False
            route = cast(Mapping[str, object], raw_route)
            usage_kind = route.get("usage_kind")
            if usage_kind not in {"model", "embedding"} or any(
                not isinstance(route.get(field), str) or not route.get(field)
                for field in (
                    "provider",
                    "model",
                    "price_source_ref",
                    "price_source_version",
                )
            ):
                return False
            if "input_token_price_usd" not in route or (
                usage_kind == "model" and "output_token_price_usd" not in route
            ):
                return False
            try:
                if route.get("input_token_price_usd") is not None:
                    _decimal(route.get("input_token_price_usd"), field="input token price")
                if usage_kind == "model" and route.get("output_token_price_usd") is not None:
                    _decimal(route.get("output_token_price_usd"), field="output token price")
            except RuntimeError:
                return False
            if usage_kind == "model":
                route_key = (cast(str, route["provider"]), cast(str, route["model"]))
                if route_key not in allowed_model_routes:
                    return False
                frozen_model_routes.append(route_key)
        if (
            len(frozen_model_routes) != len(allowed_model_routes)
            or set(frozen_model_routes) != allowed_model_routes
        ):
            return False
    return True


def _normalize_detail_values(raw: Mapping[object, object], *, allocation: bool) -> dict[str, Any]:
    detail: dict[str, Any] = {str(key): value for key, value in raw.items()}
    if not isinstance(detail.get("id"), str) or not detail["id"]:
        raise RuntimeError("0016 backfill detail id is invalid")
    if detail.get("state") not in {"reserved", "settled", "released", "needs_review"}:
        raise RuntimeError("0016 backfill detail state is invalid")
    if detail.get("side_effect_state") not in {"not_started", "started", "result_committed"}:
        raise RuntimeError("0016 backfill side-effect state is invalid")
    reserved_tokens = detail.get("reserved_tokens")
    if allocation and reserved_tokens is None:
        detail["reserved_tokens"] = None
    else:
        detail["reserved_tokens"] = _integer(reserved_tokens, field="reserved_tokens")
    actual_tokens = detail.get("actual_tokens")
    detail["actual_tokens"] = (
        None if actual_tokens is None else _integer(actual_tokens, field="actual_tokens")
    )
    for name in ("reserved_cost", "actual_cost"):
        value = detail.get(name)
        detail[name] = None if value is None else _decimal(value, field=name)
    detail["token_impact"] = _integer(detail.get("token_impact"), field="token_impact")
    detail["cost_impact"] = _decimal(detail.get("cost_impact"), field="cost_impact")
    result = detail.get("result_json")
    if result is not None and not isinstance(result, Mapping):
        raise RuntimeError("0016 backfill result is invalid")
    if detail["side_effect_state"] == "result_committed" and result is None:
        raise RuntimeError("0016 backfill committed detail lacks result")
    return detail


__all__ = [
    "_require_legacy_closed",
    "_identity_valid",
    "_usage_link_valid",
    "_snapshot_catalog_valid",
    "_normalize_detail_values",
]
