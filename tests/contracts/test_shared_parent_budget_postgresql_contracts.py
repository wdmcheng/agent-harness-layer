"""Shared parent budget ledger 的真实 PostgreSQL row-lock/CAS 合同。"""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
from tests.contracts.test_shared_parent_budget_migration_contracts import canonical_hash
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    corrupt_tree_catalog,
    create_root,
    identity,
)

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
    "cast",
    "command",
    "corrupt_tree_catalog",
    "create_async_engine",
    "create_root",
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
    "select",
    "text",
]
