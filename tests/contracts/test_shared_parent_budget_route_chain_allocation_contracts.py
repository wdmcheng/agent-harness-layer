"""Allocation route-chain 的耐久、proof 与 transfer 合同。"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select, update
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    create_delegation,
    create_root,
    sqlite_dsn,
)
from tests.contracts.test_shared_parent_budget_route_chain_repository_contracts import (
    CHAIN_USAGE_ID,
    allocation_proven_state,
    allocation_route_state,
    allocation_transferred_state,
    allocation_v2_identity,
    create_route_chain_operation,
)

from agent_harness.models._route_chain_state import close_route_attempt
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.run_models import AgentRunModel
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationConflict,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


async def assert_transfer_transition_matches_reservations(
    storage: SQLAlchemyStorage,
    *,
    run_id: str,
) -> None:
    """公开transfer seam必须拒绝transition与前后reservation任一数值冲突。"""

    started = allocation_route_state(started=True)
    proof = allocation_proven_state()
    async with storage.uow() as uow:
        await uow.shared_budget.append_model_route_attempt_started(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=CHAIN_USAGE_ID,
            state=started,
        )
        await uow.commit()
    async with storage.uow() as uow:
        await uow.shared_budget.append_model_route_not_started_proof(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=CHAIN_USAGE_ID,
            state=proof,
        )
        await uow.commit()

    for field, invalid_value in (
        ("released_token_bound", 999),
        ("released_cost_bound", 99.0),
        ("reserved_token_bound", 777),
        ("reserved_cost_bound", 77.0),
    ):
        forged = deepcopy(allocation_transferred_state().to_payload())
        forged["transitions"][-1][field] = invalid_value
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict):
                await uow.shared_budget.transfer_model_route_reservation(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=ModelRouteChainState.model_validate(forged),
                )

    async with storage.uow() as uow:
        persisted = await uow.shared_budget.get_model_route_chain_state(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=CHAIN_USAGE_ID,
        )
        direct = await uow.session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == "tenant-a",
                BudgetOperationClaimModel.usage_call_id == CHAIN_USAGE_ID,
            )
        )
        allocation = await uow.session.scalar(
            select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == "tenant-a",
                DelegationBudgetAllocationModel.usage_call_id == CHAIN_USAGE_ID,
            )
        )
        operation = direct or allocation
        operation_impact = (
            None if operation is None else (operation.token_impact, operation.reserved_tokens)
        )
    assert persisted == proof
    assert operation_impact == (20, 20)


@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.asyncio
async def test_sqlite_transfer_transition_bounds_match_previous_and_next_reservation(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """SQLite direct/allocation均在改账前拒绝transition bound自相矛盾。"""

    dsn = sqlite_dsn(tmp_path / f"route-transfer-bounds-{ownership_kind}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await create_route_chain_operation(
            storage,
            ownership_kind=ownership_kind,
            suffix=f"route-transfer-bounds-{ownership_kind}",
        )
        await assert_transfer_transition_matches_reservations(storage, run_id=run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_persists_and_replays_route_chain_started_identity(
    tmp_path: Path,
) -> None:
    """Allocation 与 direct 共用同一 state/CAS seam，started append 逐值耐久且可重放。"""

    dsn = sqlite_dsn(tmp_path / "route-chain-allocation.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root_id = await create_root(storage, suffix="route-chain-allocation")
        delegation_id, child_id = await create_delegation(
            storage,
            root_id=root_id,
            suffix="route-chain-allocation",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == child_id)
                .values(idempotency_key=f"delegation:{delegation_id}")
            )
            await uow.commit()
        initial = allocation_route_state()
        claim = AllocationBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root_id,
            delegation_id=delegation_id,
            usage_call_id=CHAIN_USAGE_ID,
            identity=allocation_v2_identity(
                root_id=root_id,
                child_id=child_id,
                delegation_id=delegation_id,
            ),
            token_reservation=20,
            cost_reservation=Decimal("1.00"),
            route_chain_state=initial,
        )
        async with storage.uow() as uow:
            created = await uow.shared_budget.allocate(claim)
            await uow.commit()
        assert created.route_chain_state == initial

        started = allocation_route_state(started=True)
        async with storage.uow() as uow:
            appended = await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=started,
            )
            await uow.commit()
        assert appended.route_chain_state == started
        async with storage.uow() as uow:
            ledger_after_append = await uow.shared_budget.get_ledger("tenant-a", root_id)
        assert ledger_after_append is not None

        async with storage.uow() as uow:
            replay = await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=started,
            )
            await uow.commit()
        assert replay.replayed is True
        assert replay.route_chain_state == started
        async with storage.uow() as uow:
            ledger_after_replay = await uow.shared_budget.get_ledger("tenant-a", root_id)
        assert ledger_after_replay is not None
        assert ledger_after_replay.version == ledger_after_append.version
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_transfer_rejects_missing_actual_zero_proof(tmp_path: Path) -> None:
    """Allocation 不能仅凭伪造 transition 越过 started lifecycle，必须保留原 reservation。"""

    dsn = sqlite_dsn(tmp_path / "route-chain-allocation-proof.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root_id = await create_root(storage, suffix="route-chain-allocation-proof")
        delegation_id, child_id = await create_delegation(
            storage,
            root_id=root_id,
            suffix="route-chain-allocation-proof",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == child_id)
                .values(idempotency_key=f"delegation:{delegation_id}")
            )
            await uow.commit()
        initial = allocation_route_state()
        claim = AllocationBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root_id,
            delegation_id=delegation_id,
            usage_call_id=CHAIN_USAGE_ID,
            identity=allocation_v2_identity(
                root_id=root_id,
                child_id=child_id,
                delegation_id=delegation_id,
            ),
            token_reservation=20,
            cost_reservation=Decimal("1.00"),
            route_chain_state=initial,
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(claim)
            await uow.commit()

        candidates = list(initial.candidates)
        candidates[0] = candidates[0].model_copy(update={"state": "not_started"})
        candidates[1] = candidates[1].model_copy(update={"state": "active"})
        reservation = initial.current_reservation.model_copy(
            update={"candidate_ordinal": 2, "token_bound": 10, "cost_bound": 0.5}
        )
        transition = initial.transitions[0].model_copy(
            update={
                "sequence": 2,
                "from_ordinal": 1,
                "to_ordinal": 2,
                "state": "transferred",
                "reason": "client_not_started",
                "released_token_bound": 20,
                "released_cost_bound": 1.0,
                "reserved_token_bound": 10,
                "reserved_cost_bound": 0.5,
            }
        )
        # 绕过 DTO 重校验，验证仓储层不会只信调用方构造的 transition。
        invalid = initial.model_copy(
            update={
                "active_ordinal": 2,
                "evidence_route_ordinal": 2,
                "candidates": tuple(candidates),
                "current_reservation": reservation,
                "transitions": (*initial.transitions, transition),
            }
        )
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict):
                await uow.shared_budget.transfer_model_route_reservation(
                    tenant_id="tenant-a",
                    run_id=child_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=invalid,
                )
        async with storage.uow() as uow:
            persisted = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
            )
        assert persisted == initial
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_atomic_proof_transfer_and_exact_replay_replace_one_reservation(
    tmp_path: Path,
) -> None:
    """Allocation 与 direct 对称：proof-close 和 A→B 替换在同一 owner UoW。"""

    dsn = sqlite_dsn(tmp_path / "route-chain-allocation-transfer.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root_id = await create_root(storage, suffix="route-chain-allocation-transfer")
        delegation_id, child_id = await create_delegation(
            storage,
            root_id=root_id,
            suffix="route-chain-allocation-transfer",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == child_id)
                .values(idempotency_key=f"delegation:{delegation_id}")
            )
            await uow.commit()
        claim = AllocationBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root_id,
            delegation_id=delegation_id,
            usage_call_id=CHAIN_USAGE_ID,
            identity=allocation_v2_identity(
                root_id=root_id,
                child_id=child_id,
                delegation_id=delegation_id,
            ),
            token_reservation=20,
            cost_reservation=Decimal("1.00"),
            route_chain_state=allocation_route_state(),
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(claim)
            await uow.commit()
        async with storage.uow() as uow:
            await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=allocation_route_state(started=True),
            )
            await uow.commit()
        proof = allocation_proven_state()
        transferred = allocation_transferred_state()
        async with storage.uow() as uow:
            result = await uow.shared_budget.prove_and_transfer_model_route_reservation(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                proof_state=proof,
                state=transferred,
            )
            await uow.commit()
        assert result.route_chain_state == transferred
        assert result.token_impact == 10
        assert result.cost_impact == Decimal("0.5")

        async with storage.uow() as uow:
            replay = await uow.shared_budget.prove_and_transfer_model_route_reservation(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                proof_state=proof,
                state=transferred,
            )
            await uow.commit()
        assert replay.replayed is True
        assert replay.route_chain_state == transferred
        assert replay.token_impact == 10
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_cancelled_actual_settlement_is_atomic_and_exactly_replayed(
    tmp_path: Path,
) -> None:
    """Allocation取消与direct共用同一终态校验，actual替换reservation且重放零改写。"""

    dsn = sqlite_dsn(tmp_path / "route-chain-allocation-cancelled.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root_id = await create_root(storage, suffix="route-chain-allocation-cancelled")
        delegation_id, child_id = await create_delegation(
            storage,
            root_id=root_id,
            suffix="route-chain-allocation-cancelled",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(AgentRunModel)
                .where(AgentRunModel.id == child_id)
                .values(idempotency_key=f"delegation:{delegation_id}")
            )
            await uow.commit()
        initial = allocation_route_state()
        claim = AllocationBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root_id,
            delegation_id=delegation_id,
            usage_call_id=CHAIN_USAGE_ID,
            identity=allocation_v2_identity(
                root_id=root_id,
                child_id=child_id,
                delegation_id=delegation_id,
            ),
            token_reservation=20,
            cost_reservation=Decimal("1.00"),
            route_chain_state=initial,
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(claim)
            await uow.commit()
        started = allocation_route_state(started=True)
        async with storage.uow() as uow:
            await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=started,
            )
            await uow.commit()

        cancelled = close_route_attempt(
            started,
            candidate_ordinal=1,
            lifecycle_state="settled",
            response_observed=False,
            request_sent=True,
            usage_observed=True,
            text_observed=False,
            completion_observed=False,
            terminal_outcome="cancelled",
        )
        result = {
            "outcome": "cancelled",
            "failure": {
                "error_code": "model.invocation_cancelled",
                "provider_called": True,
                "attempt_count": 1,
                "latency_ms": 1,
            },
            "evidence": {
                "decision": {
                    "route_chain": {
                        "state": cancelled.to_payload(),
                    }
                }
            },
        }
        async with storage.uow() as uow:
            settled = await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root_id,
                delegation_id=delegation_id,
                usage_call_id=CHAIN_USAGE_ID,
                actual_tokens=2,
                actual_cost=Decimal("0.000002"),
                cost_status="reported",
                result=result,
            )
            await uow.commit()
        assert settled.state == "settled"
        assert settled.side_effect_state == "result_committed"
        assert settled.token_impact == 2
        assert settled.route_chain_state == cancelled
        async with storage.uow() as uow:
            persisted = await uow.session.scalar(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.tenant_id == "tenant-a",
                    DelegationBudgetAllocationModel.usage_call_id == CHAIN_USAGE_ID,
                )
            )
            persisted_actual = None if persisted is None else persisted.actual_tokens
        assert persisted_actual == 2

        async with storage.uow() as uow:
            replay = await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root_id,
                delegation_id=delegation_id,
                usage_call_id=CHAIN_USAGE_ID,
                actual_tokens=2,
                actual_cost=Decimal("0.000002"),
                cost_status="reported",
                result=result,
            )
            await uow.commit()
        assert replay.replayed is True
        assert replay.route_chain_state == cancelled
        assert replay.token_impact == 2
    finally:
        await storage.dispose()
