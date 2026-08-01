"""Allocation route-chain 的未知关闭、复核与重放合同。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    create_delegation,
    create_root,
    sqlite_dsn,
)
from tests.contracts.test_shared_parent_budget_route_chain_repository_contracts import (
    CHAIN_USAGE_ID,
    allocation_route_state,
    allocation_v2_identity,
)

from agent_harness.models import ModelAttemptEvidence
from agent_harness.models._route_chain_state import close_route_attempt
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.run_models import AgentRunModel
from agent_harness.storage.shared_budget import AllocationBudgetClaim
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


@pytest.mark.asyncio
async def test_allocation_cleanup_unknown_enters_review_and_replays_exactly(
    tmp_path: Path,
) -> None:
    """Allocation 与 direct 对称保留未知 reservation，并同步围栏 top claim 与 ledger。"""

    dsn = sqlite_dsn(tmp_path / "route-chain-allocation-cleanup-review.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root_id = await create_root(storage, suffix="route-chain-allocation-cleanup-review")
        delegation_id, child_id = await create_delegation(
            storage,
            root_id=root_id,
            suffix="route-chain-allocation-cleanup-review",
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

        attempt = ModelAttemptEvidence(
            attempt=1,
            side_effect_state="unknown",
            outcome="unknown",
            completion_observed=True,
            error_code="model.provider_side_effect_unknown",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.000002,
            cost_status="reported",
            budget_charge_tokens=2,
            budget_charge_cost_usd=0.000002,
            latency_ms=1,
        )
        unknown = close_route_attempt(
            started,
            candidate_ordinal=1,
            lifecycle_state="unknown",
            response_observed=True,
            request_sent=True,
            usage_observed=True,
            text_observed=True,
            completion_observed=True,
            http_status=attempt.http_status,
            response_identity_observed=False,
        )
        review = {
            "provider_close_state": "unknown",
            "usage_finality": "complete",
            "outcome": "failed",
            "error_code": "model.provider_side_effect_unknown",
            "provider_called": True,
            "latency_ms": 1,
            "attempts": [attempt.to_payload()],
            "budget_charge": {
                "charged_tokens": None,
                "charged_cost_usd": None,
                "charge_status": "unknown",
                "unresolved_attempts": [1],
            },
        }
        result = {"attempt_review": review}
        async with storage.uow() as uow:
            await uow.shared_budget.close_model_route_attempt(
                tenant_id="tenant-a",
                run_id=child_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=unknown,
            )
            settled = await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root_id,
                delegation_id=delegation_id,
                usage_call_id=CHAIN_USAGE_ID,
                actual_tokens=None,
                actual_cost=None,
                cost_status="unavailable",
                result=result,
            )
            await uow.commit()
        assert settled.state == "needs_review"
        assert settled.side_effect_state == "result_committed"
        assert settled.result == result
        assert settled.route_chain_state == unknown

        async with storage.uow() as uow:
            top = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == delegation_id
                )
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", root_id)
            top_state = None if top is None else top.state
        assert top_state == "needs_review"
        assert ledger is not None
        assert ledger.state == "needs_review"

        async with storage.uow() as uow:
            replay = await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root_id,
                delegation_id=delegation_id,
                usage_call_id=CHAIN_USAGE_ID,
                actual_tokens=None,
                actual_cost=None,
                cost_status="unavailable",
                result=result,
            )
            await uow.commit()
        assert replay.replayed is True
        assert replay.result == result
        assert replay.route_chain_state == unknown
    finally:
        await storage.dispose()
