"""0015 delegation claim、预算预约与 migration 合同。"""

from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text, update
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_models import DelegationBudgetReservationModel
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationStorageConflict,
)
from agent_harness.storage.event_capacity_repositories import MAX_EVENT_SEQ, EventCapacityExceeded
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.shared_budget import LedgerCreate


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _create_parent(
    storage: SQLAlchemyStorage,
    *,
    suffix: str = "",
    cost_limit: Decimal | None = Decimal("10.00"),
    target_token_limit: int = 60,
    target_cost_limit: Decimal | None = Decimal("4.00"),
) -> str:
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id=f"session-a{suffix}",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-source",
            )
        )
        parent = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-source",
                trace_id=f"trace-parent{suffix}",
            )
        )
        await uow.shared_budget.create_ledger(
            LedgerCreate(
                tenant_id="tenant-a",
                budget_owner_run_id=parent.id,
                token_limit=100,
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
                        "max_tokens_per_run": 100,
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
                                    100 if agent_id == "agent-source" else target_token_limit
                                ),
                                "max_cost_usd_per_run": (
                                    None
                                    if cost_limit is None
                                    else str(
                                        cost_limit
                                        if agent_id == "agent-source"
                                        else target_cost_limit
                                    )
                                    if target_cost_limit is not None
                                    else None
                                ),
                            },
                            "routes": [
                                {
                                    "usage_kind": "model",
                                    "provider": "fake",
                                    "model": "fake-basic",
                                    "price_source_ref": "catalog:fake",
                                    "price_source_version": "v1",
                                    "input_token_price_usd": "0",
                                    "output_token_price_usd": "0",
                                    "soft_max_tokens_per_call": 100,
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


async def _create_child_relation(
    storage: SQLAlchemyStorage,
    *,
    parent_run_id: str,
    suffix: str = "",
) -> str:
    """只写既有 run 父子关系，不借 delegation claim 制造降级证据。"""

    async with storage.uow() as uow:
        child = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=f"session-a{suffix}",
                agent_id="agent-target",
                parent_run_id=parent_run_id,
                trace_id=f"trace-parent{suffix}",
            )
        )
        await uow.commit()
        return child.id


def _claim(parent_run_id: str, **updates: object) -> DelegationClaimCreate:
    payload: dict[str, object] = {
        "tenant_id": "tenant-a",
        "parent_run_id": parent_run_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "idempotency_key": "delegation-key",
        "request_hash": "a" * 64,
        "budget_intent": "inherit_parent",
        "child_input": {"query": "safe"},
        "identity": {"user_id": "user-a", "session_id": "session-a"},
        "trace_id": "trace-parent",
        "request_id": "request-a",
        "parent_token_limit": 100,
        "requested_token_reservation": 60,
        "parent_cost_limit": 10.0,
        "requested_cost_reservation": 4.0,
    }
    payload.update(updates)
    return DelegationClaimCreate.model_validate(payload)


__all__ = [
    "DelegationBudgetExceeded",
    "DelegationBudgetReservationModel",
    "DelegationClaimCreate",
    "DelegationClaimResult",
    "DelegationStorageConflict",
    "EventCapacityExceeded",
    "MAX_EVENT_SEQ",
    "Path",
    "RunCreate",
    "SQLAlchemyStorage",
    "SessionCreate",
    "_claim",
    "_create_child_relation",
    "_create_parent",
    "asyncio",
    "command",
    "migration_config",
    "pytest",
    "run_migrations",
    "sqlite3",
    "sqlite_dsn",
    "text",
    "update",
]
