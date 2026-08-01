"""共享预算 route-chain repository 的 SQLite 防篡改合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import JSON, update
from tests.contracts.test_shared_parent_budget_repository_contracts import sqlite_dsn
from tests.contracts.test_shared_parent_budget_route_chain_repository_contracts import (
    CHAIN_USAGE_ID,
    allocation_route_state,
    assert_nonterminal_mutation_preserves_reservation,
    create_route_chain_operation,
    second_started_route_state,
    waiting_route_state,
)

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.shared_budget import BudgetOperationConflict
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.asyncio
async def test_sqlite_waiting_route_chain_lookup_is_symmetric_and_ignores_json_null(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """审批恢复查询对两类 owner 对称，SQL JSON null 也不能伪装成 waiting state。"""

    dsn = sqlite_dsn(tmp_path / f"route-waiting-{ownership_kind}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await create_route_chain_operation(
            storage,
            ownership_kind=ownership_kind,
            suffix=f"waiting-{ownership_kind}",
            route_chain_state=waiting_route_state(),
        )
        async with storage.uow() as uow:
            assert await uow.shared_budget.has_waiting_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=run_id,
            )
            model_type = (
                BudgetOperationClaimModel
                if ownership_kind == "direct"
                else DelegationBudgetAllocationModel
            )
            await uow.session.execute(
                update(model_type)
                .where(model_type.usage_call_id == CHAIN_USAGE_ID)
                .values(route_chain_state_json=JSON.NULL)
            )
            await uow.commit()
        async with storage.uow() as uow:
            assert not await uow.shared_budget.has_waiting_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=run_id,
            )
    finally:
        await storage.dispose()


@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.parametrize("mutation", ["attempt_started", "proof", "delta", "close_unknown"])
@pytest.mark.asyncio
async def test_sqlite_nonterminal_route_mutations_cannot_lower_reservation(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
    mutation: Literal["attempt_started", "proof", "delta", "close_unknown"],
) -> None:
    """SQLite direct/allocation 均在改账前拒绝 started/unknown reservation 篡改。"""

    dsn = sqlite_dsn(tmp_path / f"route-reservation-{ownership_kind}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await create_route_chain_operation(
            storage,
            ownership_kind=ownership_kind,
            suffix=f"route-reservation-{ownership_kind}",
        )
        await assert_nonterminal_mutation_preserves_reservation(
            storage,
            run_id=run_id,
            mutation=mutation,
        )
    finally:
        await storage.dispose()


@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.asyncio
async def test_sqlite_next_attempt_requires_previous_lifecycle_to_be_proven(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """Direct/allocation 均拒绝在前一 started 未证明关闭时追加下一 attempt。"""

    dsn = sqlite_dsn(tmp_path / f"route-lifecycle-order-{ownership_kind}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await create_route_chain_operation(
            storage,
            ownership_kind=ownership_kind,
            suffix=f"order-{ownership_kind[0]}",
        )
        async with storage.uow() as uow:
            await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=allocation_route_state(started=True),
            )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict):
                await uow.shared_budget.append_model_route_attempt_started(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=second_started_route_state(),
                )
    finally:
        await storage.dispose()
