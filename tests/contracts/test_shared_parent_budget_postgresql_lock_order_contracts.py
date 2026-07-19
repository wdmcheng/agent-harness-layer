"""真实 PostgreSQL 的共享预算 settlement 锁序与 delegation JSON 约束。"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    allocation_identity,
    create_delegation,
    create_root,
)

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetReservationRejected,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="shared budget 锁序合同需要真实 PostgreSQL。",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_action", ["settle", "release"])
async def test_allocation_and_delegation_terminal_share_owner_first_lock_order(
    terminal_action: str,
) -> None:
    """Allocation 与 top-level terminal mutation 不得形成 ledger/claim 反向等待。"""

    async with isolated_database(f"shared_budget_{terminal_action}_allocation_lock") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            suffix = f"pg-{terminal_action}-allocation"
            root = await create_root(storage, suffix=suffix)
            delegation_id, child_id = await create_delegation(
                storage,
                root_id=root,
                suffix=suffix,
            )
            owner_locked = asyncio.Event()
            terminal_waiting_for_owner = asyncio.Event()

            async def allocate_while_holding_owner() -> str:
                async with storage.uow() as uow:
                    repository = cast(Any, uow.shared_budget)
                    await repository._lock_ledger("tenant-a", root)
                    owner_locked.set()
                    await asyncio.wait_for(terminal_waiting_for_owner.wait(), timeout=5)
                    await repository.allocate(
                        AllocationBudgetClaim(
                            tenant_id="tenant-a",
                            budget_owner_run_id=root,
                            delegation_id=delegation_id,
                            usage_call_id=f"usage-{suffix}",
                            identity=allocation_identity(
                                root_id=root,
                                child_id=child_id,
                                delegation_id=delegation_id,
                            ),
                            token_reservation=20,
                            cost_reservation=Decimal("1.00"),
                        )
                    )
                    await uow.commit()
                return "allocated"

            async def terminate_after_allocation_holds_owner() -> str:
                await asyncio.wait_for(owner_locked.wait(), timeout=5)
                try:
                    async with storage.uow() as uow:
                        repository = cast(Any, uow.shared_budget)
                        original_lock = repository._lock_ledger

                        async def observed_owner_lock(*args: Any, **kwargs: Any) -> Any:
                            terminal_waiting_for_owner.set()
                            return await original_lock(*args, **kwargs)

                        repository._lock_ledger = observed_owner_lock
                        if terminal_action == "settle":
                            await repository.settle_delegation(
                                delegation_id=delegation_id,
                                actual_tokens=20,
                                actual_cost=Decimal("1.00"),
                                cost_status="reported",
                                needs_review=False,
                                result={"aggregate_status": "complete"},
                            )
                        else:
                            await repository.release_delegation(delegation_id=delegation_id)
                        await uow.commit()
                    return terminal_action
                except BudgetReservationRejected:
                    return f"{terminal_action}-rejected"

            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    allocate_while_holding_owner(),
                    terminate_after_allocation_holds_owner(),
                ),
                timeout=10,
            )
            assert outcomes == [
                "allocated",
                "settle" if terminal_action == "settle" else "release-rejected",
            ]

            async with storage.uow() as uow:
                allocation = await uow.session.scalar(
                    select(DelegationBudgetAllocationModel).where(
                        DelegationBudgetAllocationModel.delegation_id == delegation_id
                    )
                )
                top_level = await uow.session.scalar(
                    select(BudgetOperationClaimModel).where(
                        BudgetOperationClaimModel.delegation_id == delegation_id
                    )
                )
                ledger = await uow.session.get(
                    ParentBudgetLedgerModel,
                    ("tenant-a", root),
                )
                assert allocation is not None
                assert top_level is not None
                assert ledger is not None
                top_level_state = top_level.state
                ledger_state = ledger.state
            assert top_level_state == (
                "needs_review" if terminal_action == "settle" else "reserved"
            )
            assert ledger_state == ("needs_review" if terminal_action == "settle" else "active")
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_rejects_missing_or_empty_delegation_route_digest() -> None:
    """其余字段合法时，delegation route digest 自身仍必须非空。"""

    async with isolated_database("shared_budget_delegation_route_digest") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root = await create_root(storage, suffix="pg-delegation-route-digest")
            delegation_id, _child_id = await create_delegation(
                storage,
                root_id=root,
                suffix="pg-delegation-route-digest",
            )
            for mode in ("missing", "null", "empty"):
                with pytest.raises(IntegrityError):
                    async with storage.uow() as uow:
                        claim = await uow.session.scalar(
                            select(BudgetOperationClaimModel).where(
                                BudgetOperationClaimModel.delegation_id == delegation_id
                            )
                        )
                        assert claim is not None
                        corrupted = dict(claim.identity_json)
                        if mode == "missing":
                            corrupted.pop("target_route_catalog_digest")
                        else:
                            corrupted["target_route_catalog_digest"] = (
                                None if mode == "null" else ""
                            )
                        claim.identity_json = corrupted
                        await uow.commit()
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forbidden_field",
    ["source_agent_id", "target_agent_id", "target_route_catalog_digest"],
)
async def test_postgresql_rejects_delegation_fields_on_allocation_identity(
    forbidden_field: str,
) -> None:
    """Allocation identity 不得伪装成携带 delegation 顶层路由身份。"""

    async with isolated_database(f"shared_budget_allocation_{forbidden_field}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            short_field = {
                "source_agent_id": "source",
                "target_agent_id": "target",
                "target_route_catalog_digest": "route",
            }[forbidden_field]
            suffix = f"pg-allocation-{short_field}"
            root = await create_root(storage, suffix=suffix)
            delegation_id, child_id = await create_delegation(
                storage,
                root_id=root,
                suffix=suffix,
            )
            usage_call_id = f"usage-{suffix}"
            async with storage.uow() as uow:
                await uow.shared_budget.allocate(
                    AllocationBudgetClaim(
                        tenant_id="tenant-a",
                        budget_owner_run_id=root,
                        delegation_id=delegation_id,
                        usage_call_id=usage_call_id,
                        identity=allocation_identity(
                            root_id=root,
                            child_id=child_id,
                            delegation_id=delegation_id,
                        ),
                        token_reservation=20,
                        cost_reservation=Decimal("1.00"),
                    )
                )
                await uow.commit()

            with pytest.raises(IntegrityError):
                async with storage.uow() as uow:
                    allocation = await uow.session.scalar(
                        select(DelegationBudgetAllocationModel).where(
                            DelegationBudgetAllocationModel.usage_call_id == usage_call_id
                        )
                    )
                    assert allocation is not None
                    corrupted = dict(allocation.identity_json)
                    corrupted[forbidden_field] = "forbidden-delegation-value"
                    allocation.identity_json = corrupted
                    await uow.commit()
        finally:
            await storage.dispose()
