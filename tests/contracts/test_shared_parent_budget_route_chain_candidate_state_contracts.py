"""候选聚合状态、attempt lifecycle 与审批历史的交叉不变量合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from tests.contracts.test_shared_parent_budget_repository_contracts import sqlite_dsn
from tests.contracts.test_shared_parent_budget_route_chain_repository_contracts import (
    allocation_proven_state,
    allocation_route_state,
    create_route_chain_operation,
)

from agent_harness.models._route_chain_state import close_route_attempt
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.model_route_chain_state import (
    ModelRouteChainState,
    route_chain_can_start_active_candidate,
)
from agent_harness.storage.shared_budget import BudgetOperationConflict

CandidateStateViolation = Literal[
    "approved_balance_without_waiting",
    "static_started_highwater",
    "active_started_without_lifecycle",
    "future_pending_unknown",
]
CANDIDATE_STATE_VIOLATIONS: tuple[CandidateStateViolation, ...] = (
    "approved_balance_without_waiting",
    "static_started_highwater",
    "active_started_without_lifecycle",
    "future_pending_unknown",
)


def forged_candidate_aggregate_state(
    violation_kind: CandidateStateViolation,
) -> ModelRouteChainState:
    """绕过Pydantic重校验，模拟旧坏行或公共边界收到的矛盾领域对象。"""

    state = allocation_route_state()
    candidates = list(state.candidates)
    if violation_kind in {"approved_balance_without_waiting", "static_started_highwater"}:
        source_updates: dict[str, object]
        if violation_kind == "approved_balance_without_waiting":
            source_updates = {
                "state": "budget_ineligible",
                "reason": "balance",
                "approval_request_binding_digest": "1" * 64,
                "approval_grant_binding_digest": "2" * 64,
            }
        else:
            source_updates = {
                "state": "static_ineligible",
                "reason": "static_ineligible",
                "side_effect_state": "started",
            }
        candidates[0] = candidates[0].model_copy(update=source_updates)
        candidates[1] = candidates[1].model_copy(update={"state": "active"})
        transition = state.transitions[0].model_copy(update={"to_ordinal": 2})
        reservation = state.current_reservation.model_copy(update={"candidate_ordinal": 2})
        return state.model_copy(
            update={
                "active_ordinal": 2,
                "evidence_route_ordinal": 2,
                "candidates": tuple(candidates),
                "transitions": (transition,),
                "current_reservation": reservation,
            }
        )
    target = 0 if violation_kind == "active_started_without_lifecycle" else 1
    candidates[target] = candidates[target].model_copy(
        update={"side_effect_state": "started" if target == 0 else "unknown"}
    )
    return state.model_copy(update={"candidates": tuple(candidates)})


@pytest.mark.parametrize("violation_kind", CANDIDATE_STATE_VIOLATIONS)
def test_candidate_aggregate_state_rejects_lifecycle_or_approval_history_drift(
    violation_kind: CandidateStateViolation,
) -> None:
    """exact DTO必须拒绝候选高水位、lifecycle或审批历史彼此矛盾。"""

    forged = forged_candidate_aggregate_state(violation_kind)

    with pytest.raises(ValidationError):
        ModelRouteChainState.model_validate(forged.to_payload())


@pytest.mark.parametrize(
    ("history_kind", "candidate_updates"),
    [
        ("started", {"request_sent": True}),
        ("proven", {"http_status": 418}),
        ("completed", {"usage_observed": False}),
        ("completed", {"completion_observed": False}),
    ],
)
def test_candidate_aggregate_observations_match_authoritative_lifecycle(
    history_kind: Literal["started", "proven", "completed"],
    candidate_updates: dict[str, object],
) -> None:
    """候选聚合的每个观察高水位都必须由权威 lifecycle 逐值导出。"""

    if history_kind == "started":
        state = allocation_route_state(started=True)
    elif history_kind == "proven":
        state = allocation_proven_state()
    else:
        state = close_route_attempt(
            allocation_route_state(started=True),
            candidate_ordinal=1,
            lifecycle_state="settled",
            response_observed=True,
        )
    candidates = list(state.candidates)
    candidates[0] = candidates[0].model_copy(update=candidate_updates)
    forged = state.model_copy(update={"candidates": tuple(candidates)})

    with pytest.raises(ValidationError):
        ModelRouteChainState.model_validate(forged.to_payload())


async def assert_candidate_aggregate_state_integrity(
    storage: SQLAlchemyStorage,
    *,
    ownership_kind: Literal["direct", "allocation"],
    violation_kind: CandidateStateViolation,
) -> None:
    """公开写入和恢复授权都拒绝绕过DTO构造的矛盾候选状态。"""

    forged = forged_candidate_aggregate_state(violation_kind)
    assert route_chain_can_start_active_candidate(forged) is False
    suffix = f"candidate-{CANDIDATE_STATE_VIOLATIONS.index(violation_kind)}-{ownership_kind[0]}"
    with pytest.raises((ValidationError, BudgetOperationConflict)):
        await create_route_chain_operation(
            storage,
            ownership_kind=ownership_kind,
            suffix=suffix,
            route_chain_state=forged,
        )


async def assert_started_lifecycle_begins_without_observations(
    storage: SQLAlchemyStorage,
    *,
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """公共追加边界拒绝把新started identity预置成已观察provider事实。"""

    suffix = f"started-observed-{ownership_kind[0]}"
    run_id = await create_route_chain_operation(
        storage,
        ownership_kind=ownership_kind,
        suffix=suffix,
    )
    started = allocation_route_state(started=True)
    proof_digest = "e" * 64
    proven_payload = started.to_payload()
    proven_payload["attempt_lifecycle"][0].update(
        {
            "lifecycle_state": "not_started_proven",
            "side_effect_state": "started",
            "request_sent": True,
            "http_response_observed": True,
            "http_status": 429,
            "completion_observed": False,
            "not_started_proof_digest": proof_digest,
        }
    )
    proven_payload["candidates"][0].update(
        {
            "side_effect_state": "started",
            "reason": "trusted_business_not_started",
            "request_sent": True,
            "http_response_observed": True,
            "http_status": 429,
            "completion_observed": False,
            "not_started_proofs": [
                {
                    "attempt": 1,
                    "reason": "trusted_business_not_started",
                    "side_effect_state": "started",
                    "request_sent": True,
                    "http_response_observed": True,
                    "http_status": 429,
                    "response_identity_observed": False,
                    "usage_observed": False,
                    "text_observed": False,
                    "delta_observed": False,
                    "completion_observed": False,
                    "endpoint_policy_digest": "f" * 64,
                    "classifier_ref": "status",
                    "classifier_version": "1",
                    "proof_digest": proof_digest,
                }
            ],
        }
    )
    proven = ModelRouteChainState.model_validate(proven_payload)
    async with storage.uow() as uow:
        await uow.shared_budget.append_model_route_attempt_started(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=started.usage_call_id,
            state=started,
        )
        await uow.commit()
    async with storage.uow() as uow:
        await uow.shared_budget.append_model_route_not_started_proof(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=proven.usage_call_id,
            state=proven,
        )
        await uow.commit()

    second = proven.attempt_lifecycle[-1].model_copy(
        update={
            "attempt": 2,
            "attempt_identity_digest": "9" * 64,
            "lifecycle_state": "started",
            "not_started_proof_digest": None,
        }
    )
    forged = proven.model_copy(update={"attempt_lifecycle": (*proven.attempt_lifecycle, second)})

    async with storage.uow() as uow:
        with pytest.raises(BudgetOperationConflict):
            await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=forged.usage_call_id,
                state=forged,
            )
    async with storage.uow() as uow:
        persisted = await uow.shared_budget.get_model_route_chain_state(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=forged.usage_call_id,
        )
    assert persisted == proven


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.parametrize("violation_kind", CANDIDATE_STATE_VIOLATIONS)
async def test_sqlite_candidate_aggregate_state_integrity(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
    violation_kind: CandidateStateViolation,
) -> None:
    """SQLite direct/allocation拒绝候选聚合与权威历史分裂。"""

    dsn = sqlite_dsn(tmp_path / f"candidate-{violation_kind}-{ownership_kind}.sqlite3")
    run_migrations(dsn)
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
async def test_sqlite_started_lifecycle_begins_without_observations(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """SQLite direct/allocation的新started identity只能从零观察初态开始。"""

    dsn = sqlite_dsn(tmp_path / f"started-observed-{ownership_kind}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        await assert_started_lifecycle_begins_without_observations(
            storage,
            ownership_kind=ownership_kind,
        )
    finally:
        await storage.dispose()
