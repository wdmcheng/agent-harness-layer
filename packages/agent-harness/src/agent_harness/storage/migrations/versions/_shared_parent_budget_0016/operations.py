"""0016 backfill operation evidence 归一化与完整性校验。"""

# SQLAlchemy 查询结果和 checkpoint JSON 均在逐字段运行时校验后才使用；stubs
# 无法把动态 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.delegation import (
    _delegation_reservation_matches_claim,
    _settled_delegation_aggregate_valid,
)
from agent_harness.storage.migrations.versions._shared_parent_budget_0016.identity import (
    _delegation_identity_valid,
    _identity_valid,
    _normalize_detail_values,
    _usage_link_valid,
)


def _normalize_operations(
    connection: sa.Connection,
    *,
    root: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
    claims: list[object],
    allocations: list[object],
    snapshot_id: str,
    snapshot: Mapping[str, object],
    cost_enabled: bool,
    delegation_fingerprint_proofs: Mapping[str, object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """校验 identity/linkage 并返回可写入 0016 表的归一化 evidence。"""

    normalized_claims: list[dict[str, Any]] = []
    normalized_allocations: list[dict[str, Any]] = []
    verified_delegation_proofs: set[str] = set()
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
            fingerprint_proof = delegation_fingerprint_proofs.get(str(delegation_id))
            relation = (
                connection.execute(
                    sa.text(
                        "select id,parent_run_id,source_agent_id,target_agent_id,idempotency_key,"
                        "request_hash from agent_delegations where id=:delegation_id "
                        "and tenant_id=:tenant_id and parent_run_id=:root_id"
                    ),
                    {
                        "delegation_id": delegation_id,
                        "tenant_id": root["tenant_id"],
                        "root_id": root["id"],
                    },
                )
                .mappings()
                .one_or_none()
            )
            identity = claim.get("identity_json")
            typed_relation: dict[str, object] | None = (
                None if relation is None else {str(key): value for key, value in relation.items()}
            )
            if (
                not isinstance(delegation_id, str)
                or typed_relation is None
                or not isinstance(identity, Mapping)
                or not isinstance(fingerprint_proof, Mapping)
                or not _delegation_identity_valid(
                    identity,
                    detail=claim,
                    relation=typed_relation,
                    snapshot_id=snapshot_id,
                    snapshot=snapshot,
                    cost_enabled=cost_enabled,
                    fingerprint_proof=fingerprint_proof,
                )
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
            verified_delegation_proofs.add(delegation_id)
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
    if verified_delegation_proofs != set(delegation_fingerprint_proofs):
        raise RuntimeError("0016 backfill delegation fingerprint provenance is incomplete")
    _require_complete_evidence(
        connection,
        root=root,
        claims=normalized_claims,
        allocations=normalized_allocations,
    )
    return normalized_claims, normalized_allocations


def _require_complete_evidence(
    connection: sa.Connection,
    *,
    root: Mapping[str, object],
    claims: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
) -> None:
    """要求 bundle 与 durable direct、delegation、allocation evidence 精确等价。"""

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
        str(item["usage_call_id"]) for item in claims if item.get("operation_kind") == "direct"
    }
    provided_delegations = {
        str(item["delegation_id"]) for item in claims if item.get("operation_kind") == "delegation"
    }
    provided_allocations = {
        (str(item["delegation_id"]), str(item["usage_call_id"])) for item in allocations
    }
    if (
        provided_direct != expected_direct
        or provided_delegations != expected_delegations
        or provided_allocations != expected_allocations
    ):
        raise RuntimeError("0016 backfill bundle omits or invents durable operation evidence")


__all__ = ["_normalize_operations"]
