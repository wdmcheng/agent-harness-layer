"""0016 legacy operation identity 与 durable usage linkage 校验。"""

# checkpoint 的 JSON identity 在逐字段运行时校验后才使用；SQLAlchemy stubs
# 无法把动态 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.values import (
    _canonical_hash,
    _decimal,
    _integer,
)


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
    usage_nullable_fields = (
        "source_agent_id",
        "target_agent_id",
        "target_route_catalog_digest",
    )
    for optional_field in (
        "delegation_claim_id",
        "price_source_ref",
        "price_source_version",
        "cache_key_digest",
        "trusted_cost_bound",
        *usage_nullable_fields,
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
        and all(identity.get(field) is None for field in usage_nullable_fields)
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


def _delegation_identity_valid(
    identity: Mapping[str, object],
    *,
    detail: Mapping[str, object],
    relation: Mapping[str, object],
    snapshot_id: str,
    snapshot: Mapping[str, object],
    cost_enabled: bool,
    fingerprint_proof: Mapping[str, object],
) -> bool:
    """验证 legacy top-level claim 的 0015 hash 与 0016 identity 双重上下文。"""

    identity_hash = identity.get("identity_hash")
    payload = dict(identity)
    payload.pop("identity_hash", None)
    nullable_fields = (
        "provider",
        "model",
        "price_source_ref",
        "price_source_version",
        "cache_key_digest",
        "trusted_cost_bound",
    )
    for nullable_field in nullable_fields:
        payload.setdefault(nullable_field, None)
    delegation_id = detail.get("delegation_id")
    source_agent_id = relation.get("source_agent_id")
    target_agent_id = relation.get("target_agent_id")
    agents = snapshot.get("agents")
    target_snapshot = agents.get(target_agent_id) if isinstance(agents, Mapping) else None
    routes = target_snapshot.get("routes") if isinstance(target_snapshot, Mapping) else None
    try:
        trusted_tokens = _integer(
            identity.get("trusted_token_bound"),
            field="delegation identity trusted token bound",
        )
        trusted_cost = (
            None
            if identity.get("trusted_cost_bound") is None
            else _decimal(
                identity.get("trusted_cost_bound"),
                field="delegation identity trusted cost bound",
            )
        )
    except RuntimeError:
        return False
    return bool(
        isinstance(identity_hash, str)
        and identity_hash == _canonical_hash(payload)
        and identity.get("identity_schema_version") == "budget-delegation-v1"
        and identity.get("ownership_kind") == "delegation"
        and identity.get("run_id") == relation.get("parent_run_id") == detail.get("run_id")
        and identity.get("agent_id") == identity.get("source_agent_id") == source_agent_id
        and identity.get("target_agent_id") == target_agent_id
        and identity.get("delegation_claim_id") == delegation_id == relation.get("id")
        and identity.get("usage_kind") == detail.get("usage_kind") == "delegation"
        and identity.get("operation_slot") == relation.get("idempotency_key")
        and identity.get("tree_snapshot_id") == snapshot_id
        and identity.get("agent_sub_snapshot_id") == f"{snapshot_id}:{target_agent_id}"
        and isinstance(routes, list)
        and bool(routes)
        and identity.get("target_route_catalog_digest")
        == f"budget-routes-v1:{_canonical_hash(routes)}"
        and all(identity.get(field) is None for field in nullable_fields)
        and identity.get("cost_enabled") is cost_enabled
        and cost_enabled == (trusted_cost is not None)
        and trusted_tokens == detail.get("reserved_tokens")
        and trusted_cost == detail.get("reserved_cost")
        and isinstance(identity.get("request_fingerprint"), str)
        and bool(identity.get("request_fingerprint"))
        and isinstance(identity.get("fingerprint_key_version"), str)
        and bool(identity.get("fingerprint_key_version"))
        and relation.get("request_hash") == detail.get("request_hash")
        and fingerprint_proof.get("proof_version") == "delegation-fingerprint-proof-v1"
        and fingerprint_proof.get("canonical_request_hash") == relation.get("request_hash")
        and fingerprint_proof.get("request_fingerprint") == identity.get("request_fingerprint")
        and fingerprint_proof.get("fingerprint_key_version")
        == identity.get("fingerprint_key_version")
        and fingerprint_proof.get("identity_hash") == identity_hash
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
    "_identity_valid",
    "_delegation_identity_valid",
    "_usage_link_valid",
    "_normalize_detail_values",
]
