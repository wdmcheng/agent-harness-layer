"""Shared parent budget ledger 的真实 PostgreSQL row-lock/CAS 合同。"""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from decimal import Decimal
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.run_trace_migration_test_helpers import migration_config
from tests.contracts.test_shared_parent_budget_migration_contracts import (
    canonical_hash,
    delegation_fingerprint_proofs,
)
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    corrupt_tree_catalog,
    create_root,
    identity,
)

from agent_harness.delegation.models import delegation_relation_id
from agent_harness.storage import RunCreate, SQLAlchemyStorage, get_current_revision, run_migrations
from agent_harness.storage.delegation_models import AgentDelegationModel
from agent_harness.storage.delegation_repositories import (
    DelegationClaimCreate,
    DelegationStorageConflict,
)
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetReservationRejected,
    DirectBudgetClaim,
    LedgerCreate,
    OperationIdentity,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    ParentBudgetLedgerModel,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="shared budget 并发合同需要真实 PostgreSQL。",
)


def direct_claim(
    *,
    root_id: str,
    usage_call_id: str,
    fingerprint: str,
    token_bound: int,
    cost_bound: Decimal,
) -> DirectBudgetClaim:
    """从确定性身份和冻结上界构造直接预算 claim，供行锁竞争场景复用。"""

    operation = (
        identity(run_id=root_id, fingerprint=fingerprint)
        .model_copy(
            update={
                "trusted_token_bound": token_bound,
                "trusted_cost_bound": cost_bound,
            }
        )
        .rehashed()
    )
    return DirectBudgetClaim(
        tenant_id="tenant-a",
        budget_owner_run_id=root_id,
        usage_call_id=usage_call_id,
        identity=operation,
        token_reservation=token_bound,
        cost_reservation=cost_bound,
    )


def delegation_claim(
    *,
    root_id: str,
    snapshot: dict[str, Any],
    key: str,
    request_hash: str,
    trace_id: str,
    requested_tokens: int,
    requested_cost: float | None,
) -> DelegationClaimCreate:
    """从 frozen target snapshot 派生与真实 repository 一致的顶层 identity。"""

    target = cast(dict[str, Any], cast(dict[str, Any], snapshot["agents"])["agent-b"])
    target_budget = cast(dict[str, Any], target["target_budget"])
    owner = cast(dict[str, Any], snapshot["owner"])
    token_bound = cast(int, target_budget["max_tokens_per_run"])
    raw_cost_bound = target_budget["max_cost_usd_per_run"]
    raw_owner_cost = owner["max_cost_usd_per_run"]
    effective_cost = raw_cost_bound if raw_cost_bound is not None else raw_owner_cost
    cost_bound = None if not owner["cost_enabled"] else Decimal(str(effective_cost))
    delegation_id = delegation_relation_id(
        tenant_id="tenant-a",
        parent_run_id=root_id,
        idempotency_key=key,
    )
    routes = cast(list[object], target["routes"])
    catalog_digest = hashlib.sha256(
        json.dumps(
            routes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return DelegationClaimCreate(
        delegation_id=delegation_id,
        tenant_id="tenant-a",
        parent_run_id=root_id,
        source_agent_id="agent-a",
        target_agent_id="agent-b",
        idempotency_key=key,
        request_hash=request_hash,
        budget_intent="inherit_parent",
        child_input={"query": "shared-budget-postgresql"},
        identity={"user_id": "user-a"},
        trace_id=trace_id,
        request_id=f"request-{key}",
        parent_token_limit=cast(int, owner["max_tokens_per_run"]),
        requested_token_reservation=requested_tokens,
        parent_cost_limit=(None if raw_owner_cost is None else float(raw_owner_cost)),
        requested_cost_reservation=requested_cost,
        budget_identity=OperationIdentity.from_delegation_request(
            tenant_id="tenant-a",
            fingerprint_key=b"shared-budget-postgresql-key",
            fingerprint_key_version="shared-budget-postgresql-v1",
            canonical_request_bytes=request_hash.encode("utf-8"),
            parent_run_id=root_id,
            source_agent_id="agent-a",
            target_agent_id="agent-b",
            delegation_claim_id=delegation_id,
            operation_slot=key,
            tree_snapshot_id=f"snapshot:{root_id}",
            target_sub_snapshot_id=f"snapshot:{root_id}:agent-b",
            target_route_catalog_digest=f"budget-routes-v1:{catalog_digest}",
            cost_enabled=bool(owner["cost_enabled"]),
            trusted_token_bound=token_bound,
            trusted_cost_bound=cost_bound,
        ),
    )


async def seed_postgresql_backfill_records(
    connection: Any,
    *,
    tenant_id: str,
    run_id: str,
    bundle: dict[str, Any],
    delegation_fingerprint_proofs: dict[str, Any] | None = None,
    prefix: str = "checkpoint",
) -> None:
    """写入相互独立且带内容交叉校验的 history/source/bundle checkpoints。"""

    core = deepcopy(bundle)
    ledger = cast(dict[str, Any], core["ledger"])
    snapshot = cast(dict[str, Any], ledger["snapshot"])
    agents = cast(dict[str, dict[str, Any]], snapshot["agents"])
    history_id = f"{prefix}-history"
    source_id = f"{prefix}-source"
    catalog = {
        agent_id: {
            "agent_id": agent["agent_id"],
            "model_policy": agent["model_policy"],
            "target_budget": agent["target_budget"],
            "routes": agent["routes"],
        }
        for agent_id, agent in agents.items()
    }
    fingerprint_proofs = deepcopy(delegation_fingerprint_proofs or {})
    fingerprint_proofs_hash = canonical_hash(fingerprint_proofs)
    history = {
        "history_version": "shared-budget-history-v1",
        "registry_version": ledger["registry_version"],
        "config_version": ledger["config_version"],
        "catalog_version": ledger["catalog_version"],
        "descriptor_versions": {
            agent_id: agent["descriptor_version"] for agent_id, agent in agents.items()
        },
        "catalog_hash": canonical_hash(catalog),
        "delegation_fingerprint_proofs_hash": fingerprint_proofs_hash,
    }
    content_hash = canonical_hash(core)
    source = {
        "source_version": "shared-budget-source-v1",
        "history_checkpoint_id": history_id,
        "content_hash": content_hash,
        "delegation_fingerprint_proofs_hash": fingerprint_proofs_hash,
        "delegation_fingerprint_proofs": fingerprint_proofs,
        "backfill": core,
    }
    referenced_bundle = deepcopy(core)
    referenced_bundle["source_ref"] = {
        "checkpoint_id": source_id,
        "history_checkpoint_id": history_id,
        "source_version": "shared-budget-source-v1",
        "history_version": "shared-budget-history-v1",
        "content_hash": content_hash,
        "delegation_fingerprint_proofs_hash": fingerprint_proofs_hash,
    }
    for checkpoint_id, sequence, state in (
        (history_id, 1, {"shared_budget_history_v1": history}),
        (source_id, 2, {"shared_budget_source_v1": source}),
        (f"{prefix}-bundle", 3, {"shared_budget_backfill_v1": referenced_bundle}),
    ):
        await connection.execute(
            text(
                "insert into checkpoints(id,tenant_id,run_id,sequence,resume_token,state_json) "
                "values (:id,:tenant_id,:run_id,:sequence,:resume_token,cast(:state as jsonb))"
            ),
            {
                "id": checkpoint_id,
                "tenant_id": tenant_id,
                "run_id": run_id,
                "sequence": sequence,
                "resume_token": f"{checkpoint_id}-resume",
                "state": json.dumps(state),
            },
        )


__all__ = [
    "AgentDelegationModel",
    "Any",
    "BudgetOperationClaimModel",
    "BudgetOperationConflict",
    "BudgetReservationRejected",
    "Decimal",
    "DelegationClaimCreate",
    "DelegationStorageConflict",
    "DirectBudgetClaim",
    "LedgerCreate",
    "OperationIdentity",
    "ParentBudgetLedgerModel",
    "RunCreate",
    "SQLAlchemyStorage",
    "asyncio",
    "canonical_hash",
    "delegation_fingerprint_proofs",
    "cast",
    "command",
    "corrupt_tree_catalog",
    "create_async_engine",
    "create_root",
    "delegation_claim",
    "direct_claim",
    "get_current_revision",
    "hashlib",
    "identity",
    "isolated_database",
    "json",
    "migration_config",
    "os",
    "pytest",
    "pytestmark",
    "run_migrations",
    "seed_postgresql_backfill_records",
    "select",
    "text",
]
