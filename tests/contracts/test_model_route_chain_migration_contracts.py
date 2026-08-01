"""0017 route-chain state 的 SQLite upgrade 与 evidence-aware downgrade 合同。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import update
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    SimulatedProcessCrash,
    bound_failover_invocation,
)
from tests.contracts.run_trace_migration_test_helpers import migration_config
from tests.contracts.test_shared_parent_budget_repository_contracts import create_root

from agent_harness.models import ModelProviderInvocationError, ModelRequest
from agent_harness.storage import SQLAlchemyStorage, get_current_revision, run_migrations
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


def _dsn(path: Path) -> str:
    """返回隔离 SQLite 的异步 DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def test_0017_empty_upgrade_and_downgrade_restores_0016_shape(tmp_path: Path) -> None:
    """空库可回到 0016，且两类 operation 表都不再暴露 route-chain 列。"""

    path = tmp_path / "empty.sqlite3"
    run_migrations(_dsn(path))
    assert get_current_revision(_dsn(path)) == "0017_model_route_chain_state"

    command.downgrade(migration_config(_dsn(path)), "0016_shared_parent_budget_ledger")

    assert get_current_revision(_dsn(path)) == "0016_shared_parent_budget_ledger"
    with sqlite3.connect(path) as connection:
        for table in ("budget_operation_claims", "delegation_budget_allocations"):
            columns = {row[1] for row in connection.execute(f"pragma table_info({table})")}
            assert "route_chain_state_json" not in columns


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["0016_shared_parent_budget_ledger", "-1", "head-1"],
)
async def test_0017_downgrade_to_0016_preserves_v1_shared_budget_evidence(
    tmp_path: Path,
    target: str,
) -> None:
    """显式或相对只退一版时都不能误用 0016 evidence 删除门禁。"""

    path = tmp_path / "v1-evidence.sqlite3"
    dsn = _dsn(path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        await create_root(storage, suffix="v1-evidence")
    finally:
        await storage.dispose()

    await asyncio.to_thread(
        command.downgrade,
        migration_config(dsn),
        target,
    )

    assert await asyncio.to_thread(get_current_revision, dsn) == "0016_shared_parent_budget_ledger"
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from parent_budget_ledgers").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_ledger_state"),
    [
        ("completed", "active"),
        ("cancelled", "active"),
        ("crash_before_send", "active"),
        ("ambiguous_timeout", "needs_review"),
    ],
)
async def test_0017_refuses_completed_active_and_needs_review_chain_state_downgrade(
    tmp_path: Path,
    outcome: str,
    expected_ledger_state: str,
) -> None:
    """任何非空 chain state 都是不可丢弃证据，不因 ledger 生命周期不同而降级。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: [
                "cancelled_on_iterate_stopped_complete" if outcome == "cancelled" else outcome
            ],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    request = ModelRequest(
        capability="text_stream" if outcome == "cancelled" else "text_completion",
        prompt="migration fence",
        max_output_tokens=8,
    )
    try:
        if outcome == "completed":
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        elif outcome == "cancelled":
            with pytest.raises(ModelProviderInvocationError, match="model.invocation_cancelled"):
                await fixture.bound.stream(request, operation_key=fixture.operation_key)
        elif outcome == "crash_before_send":
            with pytest.raises(SimulatedProcessCrash):
                await fixture.bound.complete(request, operation_key=fixture.operation_key)
        else:
            with pytest.raises(ModelProviderInvocationError):
                await fixture.bound.complete(request, operation_key=fixture.operation_key)
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert state is not None
        assert ledger is not None
        assert ledger.state == expected_ledger_state
    finally:
        await fixture.storage.dispose()

    with pytest.raises(RuntimeError, match=r"^storage\.route_chain_state_present$"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(_dsn(tmp_path / "failover.db")),
            "0016_shared_parent_budget_ledger",
        )
    assert await asyncio.to_thread(get_current_revision, _dsn(tmp_path / "failover.db")) == (
        "0017_model_route_chain_state"
    )


@pytest.mark.asyncio
async def test_0017_refuses_v2_identity_even_if_state_column_is_null(tmp_path: Path) -> None:
    """手工清空 state 不能把 v2 operation 伪装成可安全降级的 legacy row。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        await fixture.bound.complete(
            ModelRequest(prompt="migration identity fence", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )
        async with fixture.storage.uow() as uow:
            await uow.session.execute(
                update(BudgetOperationClaimModel)
                .where(BudgetOperationClaimModel.usage_call_id == fixture.usage_call_id)
                .values(route_chain_state_json=None)
            )
            await uow.commit()
    finally:
        await fixture.storage.dispose()

    with pytest.raises(RuntimeError, match=r"^storage\.route_chain_state_present$"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(_dsn(tmp_path / "failover.db")),
            "0016_shared_parent_budget_ledger",
        )
