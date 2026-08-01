"""初始与审批 transition 的 reservation 交叉不变量合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from tests.contracts.test_shared_parent_budget_repository_contracts import sqlite_dsn
from tests.contracts.test_shared_parent_budget_route_chain_repository_contracts import (
    CHAIN_USAGE_ID,
    allocation_proven_state,
    allocation_route_state,
    allocation_transferred_state,
    create_route_chain_operation,
)

from agent_harness.models._route_chain_state import wait_for_route_approval
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.model_route_chain_state import (
    ModelRouteChainState,
    route_chain_can_start_active_candidate,
)
from agent_harness.storage.shared_budget import BudgetOperationConflict

IntegrityViolation = Literal[
    "initial_bounds",
    "initial_pending_prefix",
    "approved_bounds",
    "approved_missing_grant",
    "approved_request_rebound",
]
INTEGRITY_VIOLATIONS: tuple[IntegrityViolation, ...] = (
    "initial_bounds",
    "initial_pending_prefix",
    "approved_bounds",
    "approved_missing_grant",
    "approved_request_rebound",
)


def test_recovery_rejects_collapsed_transfer_history() -> None:
    """恢复门禁拒绝把已完成的 A→B 迁移伪造成单条初始激活。"""

    payload = allocation_transferred_state().to_payload()
    reservation = payload["current_reservation"]
    active_ordinal = payload["active_ordinal"]
    payload["transitions"] = [
        {
            "sequence": 1,
            "from_ordinal": None,
            "to_ordinal": active_ordinal,
            "state": "activated",
            "reason": "initial",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": reservation["token_bound"],
            "reserved_cost_bound": reservation["cost_bound"],
        }
    ]

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


def test_recovery_rejects_duplicate_transfer_history_without_initial_anchor() -> None:
    """恢复门禁拒绝用重复 A→B 迁移冒充缺失的初始 source anchor。"""

    payload = allocation_transferred_state().to_payload()
    first_transfer = deepcopy(payload["transitions"][-1])
    first_transfer["sequence"] = 1
    second_transfer = deepcopy(first_transfer)
    second_transfer["sequence"] = 2
    payload["transitions"] = [first_transfer, second_transfer]

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


def test_recovery_rejects_transition_history_that_moves_to_an_earlier_ordinal() -> None:
    """恢复门禁拒绝连续但从 B 回跳 A 的 source-anchor 历史。"""

    payload = allocation_transferred_state().to_payload()
    payload["active_ordinal"] = 1
    payload["evidence_route_ordinal"] = 1
    payload["current_reservation"] = {
        "candidate_ordinal": 1,
        "token_bound": 10,
        "cost_bound": 0.5,
    }
    payload["candidates"][0]["state"] = "active"
    payload["candidates"][1]["state"] = "pending"
    payload["transitions"].append(
        {
            "sequence": 3,
            "from_ordinal": 2,
            "to_ordinal": 1,
            "state": "transferred",
            "reason": "client_not_started",
            "released_token_bound": 10,
            "released_cost_bound": 0.5,
            "reserved_token_bound": 10,
            "reserved_cost_bound": 0.5,
        }
    )

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


def test_recovery_rejects_transition_history_that_restarts_after_terminal() -> None:
    """恢复门禁把 terminated 视为吸收终态，不允许从 null 重新开链。"""

    payload = allocation_route_state().to_payload()
    payload["active_ordinal"] = 2
    payload["evidence_route_ordinal"] = 2
    payload["current_reservation"] = {
        "candidate_ordinal": 2,
        "token_bound": 10,
        "cost_bound": 0.5,
    }
    payload["candidates"][0].update({"state": "budget_ineligible", "reason": "balance"})
    payload["candidates"][1].update(
        {
            "state": "active",
            "approval_request_binding_digest": "1" * 64,
            "approval_grant_binding_digest": "2" * 64,
        }
    )
    payload["transitions"] = [
        payload["transitions"][0],
        {
            "sequence": 2,
            "from_ordinal": 1,
            "to_ordinal": None,
            "state": "terminated",
            "reason": "route_exhausted",
            "released_token_bound": 20,
            "released_cost_bound": 1.0,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        },
        {
            "sequence": 3,
            "from_ordinal": None,
            "to_ordinal": 2,
            "state": "waiting_approval",
            "reason": "approval_required",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        },
        {
            "sequence": 4,
            "from_ordinal": 2,
            "to_ordinal": 2,
            "state": "approved",
            "reason": "approval_granted",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 10,
            "reserved_cost_bound": 0.5,
        },
    ]

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


@pytest.mark.parametrize("history_kind", ["approved_without_waiting", "duplicate_approved"])
def test_recovery_rejects_noncanonical_approval_history(
    history_kind: Literal["approved_without_waiting", "duplicate_approved"],
) -> None:
    """恢复门禁要求 approved 唯一且紧跟同ordinal waiting。"""

    payload = allocation_route_state().to_payload()
    payload["candidates"][0].update(
        {
            "approval_request_binding_digest": "1" * 64,
            "approval_grant_binding_digest": "2" * 64,
        }
    )
    approved = {
        "sequence": 2,
        "from_ordinal": 1,
        "to_ordinal": 1,
        "state": "approved",
        "reason": "approval_granted",
        "released_token_bound": 0,
        "released_cost_bound": None,
        "reserved_token_bound": 20,
        "reserved_cost_bound": 1.0,
    }
    if history_kind == "approved_without_waiting":
        payload["transitions"].append(approved)
    else:
        waiting = {
            "sequence": 1,
            "from_ordinal": None,
            "to_ordinal": 1,
            "state": "waiting_approval",
            "reason": "approval_required",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        }
        approved["sequence"] = 2
        duplicate = deepcopy(approved)
        duplicate["sequence"] = 3
        payload["transitions"] = [waiting, approved, duplicate]

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


def test_recovery_rejects_waiting_approval_self_loop() -> None:
    """恢复门禁拒绝在同一source anchor伪造waiting自环。"""

    payload = allocation_route_state().to_payload()
    payload["candidates"][0].update(
        {
            "approval_request_binding_digest": "1" * 64,
            "approval_grant_binding_digest": "2" * 64,
        }
    )
    payload["transitions"].extend(
        [
            {
                "sequence": 2,
                "from_ordinal": 1,
                "to_ordinal": 1,
                "state": "waiting_approval",
                "reason": "approval_required",
                "released_token_bound": 0,
                "released_cost_bound": None,
                "reserved_token_bound": 0,
                "reserved_cost_bound": None,
            },
            {
                "sequence": 3,
                "from_ordinal": 1,
                "to_ordinal": 1,
                "state": "approved",
                "reason": "approval_granted",
                "released_token_bound": 0,
                "released_cost_bound": None,
                "reserved_token_bound": 20,
                "reserved_cost_bound": 1.0,
            },
        ]
    )

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


HistorySemanticViolation = Literal[
    "waiting_without_approval",
    "balance_without_bindings",
    "static_source_without_proof",
    "soft_budget_transition",
]


@pytest.mark.parametrize(
    "history_kind",
    [
        "waiting_without_approval",
        "balance_without_bindings",
        "static_source_without_proof",
        "soft_budget_transition",
    ],
)
def test_recovery_rejects_transition_source_semantic_drift(
    history_kind: HistorySemanticViolation,
) -> None:
    """恢复门禁逐值绑定waiting、proof与balance source语义。"""

    if history_kind == "waiting_without_approval":
        payload = allocation_transferred_state().to_payload()
        payload["candidates"][0]["approval_request_binding_digest"] = "1" * 64
        payload["transitions"][0].update(
            {
                "state": "waiting_approval",
                "reason": "approval_required",
                "reserved_token_bound": 0,
                "reserved_cost_bound": None,
            }
        )
    else:
        payload = allocation_route_state().to_payload()
        payload["active_ordinal"] = 2
        payload["evidence_route_ordinal"] = 2
        payload["current_reservation"] = {
            "candidate_ordinal": 2,
            "token_bound": 10,
            "cost_bound": 0.5,
        }
        payload["candidates"][1]["state"] = "active"
        source_state = (
            "static_ineligible"
            if history_kind == "static_source_without_proof"
            else "budget_ineligible"
        )
        source_reason = (
            "static_ineligible"
            if history_kind == "static_source_without_proof"
            else "balance"
            if history_kind == "balance_without_bindings"
            else "soft_budget"
        )
        payload["candidates"][0].update({"state": source_state, "reason": source_reason})
        if history_kind == "balance_without_bindings":
            payload["transitions"][0].update(
                {
                    "state": "waiting_approval",
                    "reason": "approval_required",
                    "reserved_token_bound": 0,
                    "reserved_cost_bound": None,
                }
            )
        payload["transitions"].append(
            {
                "sequence": 2,
                "from_ordinal": 1,
                "to_ordinal": 2,
                "state": "transferred",
                "reason": (
                    "client_not_started"
                    if history_kind == "static_source_without_proof"
                    else source_reason
                ),
                "released_token_bound": 0,
                "released_cost_bound": None,
                "reserved_token_bound": 10,
                "reserved_cost_bound": 0.5,
            }
        )

    if history_kind in {"balance_without_bindings", "soft_budget_transition"}:
        with pytest.raises(ValidationError):
            ModelRouteChainState.model_validate(payload)
        return

    state = ModelRouteChainState.model_validate(payload)

    assert route_chain_can_start_active_candidate(state) is False


async def assert_initial_and_approved_state_integrity(
    storage: SQLAlchemyStorage,
    *,
    ownership_kind: Literal["direct", "allocation"],
    violation_kind: IntegrityViolation,
) -> None:
    """公开创建、审批与恢复 seam 均拒绝越序或binding/bounds分裂。"""

    suffix = f"ri-{INTEGRITY_VIOLATIONS.index(violation_kind)}-{ownership_kind[0]}"
    if violation_kind.startswith("initial_"):
        forged_initial = deepcopy(allocation_route_state().to_payload())
        if violation_kind == "initial_bounds":
            forged_initial["transitions"][0]["reserved_token_bound"] = 707
            forged_initial["transitions"][0]["reserved_cost_bound"] = 70.7
        else:
            forged_initial["active_ordinal"] = 2
            forged_initial["evidence_route_ordinal"] = 2
            forged_initial["current_reservation"] = {
                "candidate_ordinal": 2,
                "token_bound": 20,
                "cost_bound": 1.0,
            }
            forged_initial["candidates"][0]["state"] = "pending"
            forged_initial["candidates"][1]["state"] = "active"
            forged_initial["transitions"][0].update(
                {"to_ordinal": 2, "reserved_token_bound": 20, "reserved_cost_bound": 1.0}
            )
        state = ModelRouteChainState.model_validate(forged_initial)
        if violation_kind == "initial_pending_prefix":
            assert route_chain_can_start_active_candidate(state) is False
        with pytest.raises(BudgetOperationConflict):
            await create_route_chain_operation(
                storage,
                ownership_kind=ownership_kind,
                suffix=suffix,
                route_chain_state=state,
            )
        return

    run_id = await create_route_chain_operation(
        storage,
        ownership_kind=ownership_kind,
        suffix=suffix,
    )
    started = allocation_route_state(started=True)
    proof = allocation_proven_state()
    async with storage.uow() as uow:
        await uow.shared_budget.append_model_route_attempt_started(
            tenant_id="tenant-a", run_id=run_id, usage_call_id=CHAIN_USAGE_ID, state=started
        )
        await uow.commit()
    async with storage.uow() as uow:
        await uow.shared_budget.append_model_route_not_started_proof(
            tenant_id="tenant-a", run_id=run_id, usage_call_id=CHAIN_USAGE_ID, state=proof
        )
        await uow.commit()
    waiting = wait_for_route_approval(
        proof,
        target_ordinal=2,
        approval_request_binding_digest="1" * 64,
    )
    async with storage.uow() as uow:
        await uow.shared_budget.transfer_model_route_reservation(
            tenant_id="tenant-a", run_id=run_id, usage_call_id=CHAIN_USAGE_ID, state=waiting
        )
        await uow.commit()
    async with storage.uow() as uow:
        ownership = await uow.shared_budget.resolve_operation_ownership(
            tenant_id="tenant-a", run_id=run_id
        )
        before_budget = await uow.shared_budget.get_ledger(
            "tenant-a", ownership.budget_owner_run_id
        )
    assert before_budget is not None
    before_impact = (before_budget.token_impact, before_budget.cost_impact)

    forged_approved = deepcopy(waiting.to_payload())
    forged_approved["candidates"][1].update(
        {"state": "active", "reason": None, "approval_grant_binding_digest": "2" * 64}
    )
    forged_approved["waiting_approval_ordinal"] = None
    forged_approved["active_ordinal"] = 2
    forged_approved["current_reservation"] = {
        "candidate_ordinal": 2,
        "token_bound": 10,
        "cost_bound": 0.5,
    }
    forged_approved["transitions"].append(
        {
            "sequence": len(waiting.transitions) + 1,
            "from_ordinal": 2,
            "to_ordinal": 2,
            "state": "approved",
            "reason": "approval_granted",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 10,
            "reserved_cost_bound": 0.5,
        }
    )
    if violation_kind == "approved_bounds":
        forged_approved["transitions"][-1]["reserved_token_bound"] = 313
        forged_approved["transitions"][-1]["reserved_cost_bound"] = 31.3
    elif violation_kind == "approved_missing_grant":
        forged_approved["candidates"][1]["approval_grant_binding_digest"] = None
    else:
        forged_approved["candidates"][1]["approval_request_binding_digest"] = "3" * 64
    approved_state = ModelRouteChainState.model_validate(forged_approved)
    if violation_kind == "approved_missing_grant":
        assert route_chain_can_start_active_candidate(approved_state) is False
    async with storage.uow() as uow:
        with pytest.raises(BudgetOperationConflict):
            await uow.shared_budget.activate_approved_model_route(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=CHAIN_USAGE_ID,
                state=approved_state,
            )
    async with storage.uow() as uow:
        persisted = await uow.shared_budget.get_model_route_chain_state(
            tenant_id="tenant-a", run_id=run_id, usage_call_id=CHAIN_USAGE_ID
        )
        ownership = await uow.shared_budget.resolve_operation_ownership(
            tenant_id="tenant-a", run_id=run_id
        )
        budget = await uow.shared_budget.get_ledger("tenant-a", ownership.budget_owner_run_id)
    assert persisted == waiting
    assert budget is not None
    assert (budget.token_impact, budget.cost_impact) == before_impact


@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.parametrize("violation_kind", INTEGRITY_VIOLATIONS)
@pytest.mark.asyncio
async def test_sqlite_initial_and_approved_state_integrity(
    tmp_path: Path,
    ownership_kind: Literal["direct", "allocation"],
    violation_kind: IntegrityViolation,
) -> None:
    """SQLite direct/allocation 创建、审批与恢复必须闭合。"""

    dsn = sqlite_dsn(tmp_path / f"route-{violation_kind}-{ownership_kind}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        await assert_initial_and_approved_state_integrity(
            storage,
            ownership_kind=ownership_kind,
            violation_kind=violation_kind,
        )
    finally:
        await storage.dispose()
