"""候选聚合状态交叉不变量的真实 PostgreSQL 合同。"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

import pytest
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.test_shared_parent_budget_route_chain_candidate_state_contracts import (
    CANDIDATE_STATE_VIOLATIONS,
    CandidateStateViolation,
    assert_candidate_aggregate_state_integrity,
    assert_started_lifecycle_begins_without_observations,
)

from agent_harness.storage import SQLAlchemyStorage, run_migrations

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="候选聚合状态合同需要真实 PostgreSQL。",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.parametrize("violation_kind", CANDIDATE_STATE_VIOLATIONS)
async def test_postgresql_candidate_aggregate_state_integrity(
    ownership_kind: Literal["direct", "allocation"],
    violation_kind: CandidateStateViolation,
) -> None:
    """真实PostgreSQL也拒绝候选聚合与权威历史分裂。"""

    async with isolated_database(f"candidate_{violation_kind}_{ownership_kind}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            await assert_candidate_aggregate_state_integrity(
                storage,
                ownership_kind=ownership_kind,
                violation_kind=violation_kind,
            )
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
async def test_postgresql_started_lifecycle_begins_without_observations(
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """真实PostgreSQL的新started identity同样只能从零观察初态开始。"""

    async with isolated_database(f"started_observed_{ownership_kind}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            await assert_started_lifecycle_begins_without_observations(
                storage,
                ownership_kind=ownership_kind,
            )
        finally:
            await storage.dispose()
