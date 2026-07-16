"""真实 PostgreSQL 下 delegation parent lock、并发幂等与预算竞争合同。"""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationStorageConflict,
)
from agent_harness.storage.repositories import RunCreate, SessionCreate

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="delegation 并发合同需要真实 PostgreSQL。",
)


async def _parent(storage: SQLAlchemyStorage, *, suffix: str) -> str:
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
    parent_cost_limit: float | None = None,
    requested_cost_reservation: float | None = None,
) -> DelegationClaimCreate:
    return DelegationClaimCreate(
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
    )


__all__ = [
    "DelegationBudgetExceeded",
    "DelegationClaimCreate",
    "DelegationClaimResult",
    "DelegationStorageConflict",
    "RunCreate",
    "SQLAlchemyStorage",
    "SessionCreate",
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
