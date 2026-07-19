"""真实 PostgreSQL 下 delegation parent lock、并发幂等与预算竞争合同。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.delegation.models import delegation_relation_id
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationStorageConflict,
)
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.shared_budget import LedgerCreate, OperationIdentity

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="delegation 并发合同需要真实 PostgreSQL。",
)


async def _parent(
    storage: SQLAlchemyStorage,
    *,
    suffix: str,
    token_limit: int = 100,
    target_token_limit: int = 60,
    cost_limit: Decimal | None = Decimal("10.00"),
    target_cost_limit: Decimal | None = None,
) -> str:
    """创建带完整 0016 frozen tree snapshot 的真实 delegation root。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure(f"tenant-{suffix}")
        session = await uow.sessions.create(
            SessionCreate(
                tenant_id=f"tenant-{suffix}",
                user_id="user-a",
                agent_id="agent-source",
            )
        )
        parent = await uow.runs.create(
            RunCreate(
                tenant_id=f"tenant-{suffix}",
                session_id=session.id,
                agent_id="agent-source",
                trace_id=f"trace-{suffix}",
            )
        )
        await uow.shared_budget.create_ledger(
            LedgerCreate(
                tenant_id=f"tenant-{suffix}",
                budget_owner_run_id=parent.id,
                token_limit=token_limit,
                cost_limit=cost_limit,
                registry_version="registry-v1",
                config_version="config-v1",
                catalog_version="catalog-v1",
                snapshot_id=f"snapshot:{parent.id}",
                snapshot={
                    "owner": {
                        "agent_id": "agent-source",
                        "root_run_id": parent.id,
                        "delegation_targets": ["agent-target"],
                        "max_tokens_per_run": token_limit,
                        "max_cost_usd_per_run": (None if cost_limit is None else str(cost_limit)),
                        "cost_enabled": cost_limit is not None,
                    },
                    "registry_version": "registry-v1",
                    "config_version": "config-v1",
                    "catalog_version": "catalog-v1",
                    "agents": {
                        agent_id: {
                            "agent_id": agent_id,
                            "descriptor_version": f"{agent_id}-v1",
                            "model_policy": {
                                "provider": "fake",
                                "default_model": "fake-basic",
                                "fallback_models": [],
                            },
                            "target_budget": {
                                "max_tokens_per_run": (
                                    token_limit
                                    if agent_id == "agent-source"
                                    else target_token_limit
                                ),
                                "max_cost_usd_per_run": (
                                    None
                                    if cost_limit is None
                                    else (
                                        str(cost_limit)
                                        if agent_id == "agent-source"
                                        else (
                                            None
                                            if target_cost_limit is None
                                            else str(target_cost_limit)
                                        )
                                    )
                                ),
                            },
                            "routes": [
                                {
                                    "usage_kind": "model",
                                    "provider": "fake",
                                    "model": "fake-basic",
                                    "price_source_ref": "price:fake",
                                    "price_source_version": "v1",
                                    "input_token_price_usd": "0",
                                    "output_token_price_usd": "0",
                                    "soft_max_tokens_per_call": token_limit,
                                }
                            ],
                        }
                        for agent_id in ("agent-source", "agent-target")
                    },
                },
            )
        )
        await uow.commit()
        return parent.id


def _claim(
    parent_run_id: str,
    *,
    suffix: str,
    key: str,
    request_hash: str,
    reserved_tokens: int,
    parent_limit: int = 100,
    parent_cost_limit: float | None = 10.0,
    requested_cost_reservation: float | None = 10.0,
    trusted_token_bound: int = 60,
) -> DelegationClaimCreate:
    """构造与 frozen tree 和路由目录摘要一致的 PG delegation claim 夹具。"""

    delegation_id = delegation_relation_id(
        tenant_id=f"tenant-{suffix}",
        parent_run_id=parent_run_id,
        idempotency_key=key,
    )
    routes = [
        {
            "usage_kind": "model",
            "provider": "fake",
            "model": "fake-basic",
            "price_source_ref": "price:fake",
            "price_source_version": "v1",
            "input_token_price_usd": "0",
            "output_token_price_usd": "0",
            "soft_max_tokens_per_call": parent_limit,
        }
    ]
    catalog_digest = hashlib.sha256(
        json.dumps(
            routes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    trusted_cost = None if parent_cost_limit is None else Decimal(str(parent_cost_limit))
    return DelegationClaimCreate(
        delegation_id=delegation_id,
        tenant_id=f"tenant-{suffix}",
        parent_run_id=parent_run_id,
        source_agent_id="agent-source",
        target_agent_id="agent-target",
        idempotency_key=key,
        request_hash=request_hash,
        budget_intent="inherit_parent",
        child_input={"prompt": "safe"},
        identity={"user_id": "user-a"},
        trace_id=f"trace-{suffix}",
        parent_token_limit=parent_limit,
        requested_token_reservation=reserved_tokens,
        parent_cost_limit=parent_cost_limit,
        requested_cost_reservation=requested_cost_reservation,
        budget_identity=OperationIdentity.from_delegation_request(
            tenant_id=f"tenant-{suffix}",
            fingerprint_key=b"postgres-delegation-contract-key",
            fingerprint_key_version="postgres-delegation-v1",
            canonical_request_bytes=request_hash.encode("utf-8"),
            parent_run_id=parent_run_id,
            source_agent_id="agent-source",
            target_agent_id="agent-target",
            delegation_claim_id=delegation_id,
            operation_slot=key,
            tree_snapshot_id=f"snapshot:{parent_run_id}",
            target_sub_snapshot_id=f"snapshot:{parent_run_id}:agent-target",
            target_route_catalog_digest=f"budget-routes-v1:{catalog_digest}",
            cost_enabled=parent_cost_limit is not None,
            trusted_token_bound=trusted_token_bound,
            trusted_cost_bound=trusted_cost,
        ),
    )


__all__ = [
    "DelegationBudgetExceeded",
    "DelegationClaimCreate",
    "DelegationClaimResult",
    "DelegationStorageConflict",
    "RunCreate",
    "SQLAlchemyStorage",
    "SessionCreate",
    "Decimal",
    "_claim",
    "_parent",
    "asyncio",
    "command",
    "create_async_engine",
    "isolated_database",
    "migration_config",
    "os",
    "pytest",
    "pytestmark",
    "run_migrations",
    "text",
]
