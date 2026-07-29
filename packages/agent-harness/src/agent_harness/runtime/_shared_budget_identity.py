"""Shared-budget operation/delegation 身份生成私有 seam。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from agent_harness.runtime._shared_budget_common import digest
from agent_harness.storage.shared_budget import OperationIdentity


def operation_identity(
    *,
    fingerprint_key: bytes,
    fingerprint_key_version: str,
    values: dict[str, Any],
) -> OperationIdentity:
    """只消费组合根已通过 CFG-001 校验的进程内 secret bytes。"""

    return OperationIdentity.from_semantic_request(
        fingerprint_key=fingerprint_key,
        fingerprint_key_version=fingerprint_key_version,
        **values,
    )


def delegation_identity(
    *,
    fingerprint_key: bytes,
    fingerprint_key_version: str,
    tenant_id: str,
    canonical_request_bytes: bytes,
    parent_run_id: str,
    source_agent_id: str,
    target_agent_id: str,
    delegation_id: str,
    idempotency_key: str,
    tree_snapshot_id: str,
    snapshot: dict[str, Any],
    trusted_token_bound: int,
    trusted_cost_bound: Decimal | None,
) -> OperationIdentity:
    """把 frozen target catalog 与 canonical request 绑定为顶层 identity。"""

    raw_owner = snapshot.get("owner")
    raw_agents = snapshot.get("agents")
    if not isinstance(raw_owner, dict) or not isinstance(raw_agents, dict):
        raise ValueError("shared budget delegation snapshot is invalid")
    owner = cast(dict[str, object], raw_owner)
    agents = cast(dict[str, object], raw_agents)
    raw_target = agents.get(target_agent_id)
    raw_targets = owner.get("delegation_targets")
    if (
        owner.get("agent_id") != source_agent_id
        or owner.get("root_run_id") != parent_run_id
        or not isinstance(raw_targets, list)
        or target_agent_id not in raw_targets
        or not isinstance(raw_target, dict)
    ):
        raise ValueError("shared budget delegation snapshot is invalid")
    target = cast(dict[str, object], raw_target)
    raw_routes = target.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError("shared budget delegation target catalog is invalid")
    routes = cast(list[object], raw_routes)
    cost_enabled = owner.get("cost_enabled")
    if not isinstance(cost_enabled, bool):
        raise ValueError("shared budget delegation cost mode is invalid")
    return OperationIdentity.from_delegation_request(
        tenant_id=tenant_id,
        fingerprint_key=fingerprint_key,
        fingerprint_key_version=fingerprint_key_version,
        canonical_request_bytes=canonical_request_bytes,
        parent_run_id=parent_run_id,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        delegation_claim_id=delegation_id,
        operation_slot=idempotency_key,
        tree_snapshot_id=tree_snapshot_id,
        target_sub_snapshot_id=f"{tree_snapshot_id}:{target_agent_id}",
        target_route_catalog_digest=f"budget-routes-v1:{digest(routes)}",
        cost_enabled=cost_enabled,
        trusted_token_bound=trusted_token_bound,
        trusted_cost_bound=trusted_cost_bound,
    )


def delegation_replay_identity(
    *,
    fingerprint_key: bytes,
    fingerprint_key_version: str,
    tenant_id: str,
    canonical_request_bytes: bytes,
    parent_run_id: str,
    source_agent_id: str,
    target_agent_id: str,
    delegation_id: str,
    idempotency_key: str,
    persisted_identity: OperationIdentity,
) -> OperationIdentity:
    """只用 durable immutable fields 重算请求身份，不依赖当前 snapshot。"""

    if (
        persisted_identity.ownership_kind != "delegation"
        or persisted_identity.target_route_catalog_digest is None
    ):
        raise ValueError("shared budget delegation replay identity is invalid")
    return OperationIdentity.from_delegation_request(
        tenant_id=tenant_id,
        fingerprint_key=fingerprint_key,
        fingerprint_key_version=fingerprint_key_version,
        canonical_request_bytes=canonical_request_bytes,
        parent_run_id=parent_run_id,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        delegation_claim_id=delegation_id,
        operation_slot=idempotency_key,
        tree_snapshot_id=persisted_identity.tree_snapshot_id,
        target_sub_snapshot_id=persisted_identity.agent_sub_snapshot_id,
        target_route_catalog_digest=persisted_identity.target_route_catalog_digest,
        cost_enabled=persisted_identity.cost_enabled,
        trusted_token_bound=persisted_identity.trusted_token_bound,
        trusted_cost_bound=persisted_identity.trusted_cost_bound,
    )


__all__ = ["delegation_identity", "delegation_replay_identity", "operation_identity"]
