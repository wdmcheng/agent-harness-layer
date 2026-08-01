"""共享预算 route-chain mutation 的封闭单调性校验。"""

from __future__ import annotations

from agent_harness.storage.model_route_chain_state import (
    ModelRouteAttemptLifecycle,
    ModelRouteCandidateState,
    ModelRouteChainState,
    candidate_has_no_approval_bindings,
    candidate_has_zero_provider_facts,
)
from agent_harness.storage.shared_budget import BudgetOperationConflict

_INITIAL_SKIP_STATES = frozenset({"static_ineligible", "budget_ineligible"})


def _is_canonical_started_identity(lifecycle: ModelRouteAttemptLifecycle) -> bool:
    """新attempt identity只记录调用意图，provider观察必须由后续mutation提升。"""

    return (
        lifecycle.lifecycle_state == "started"
        and lifecycle.side_effect_state == "not_started"
        and not lifecycle.request_sent
        and not lifecycle.http_response_observed
        and lifecycle.http_status is None
        and not lifecycle.response_identity_observed
        and not lifecycle.usage_observed
        and not lifecycle.text_observed
        and not lifecycle.delta_observed
        and lifecycle.completion_observed is None
        and lifecycle.not_started_proof_digest is None
    )


def _is_initial_skip(candidate: ModelRouteCandidateState) -> bool:
    """初始普通skip尚未经过审批，必须是零副作用且无binding。"""

    return (
        candidate.state in _INITIAL_SKIP_STATES
        and candidate_has_zero_provider_facts(candidate)
        and candidate_has_no_approval_bindings(candidate)
    )


def _is_initial_pending(candidate: ModelRouteCandidateState) -> bool:
    """尚未扫描的后继必须保持exact pending初态。"""

    return (
        candidate.state == "pending"
        and candidate.reason is None
        and candidate_has_zero_provider_facts(candidate)
        and candidate_has_no_approval_bindings(candidate)
    )


def _initial_candidate_order_matches(state: ModelRouteChainState, target_ordinal: int) -> bool:
    """初态只能跨过已分类前缀，目标之后仍必须保持未决。"""

    return (
        state.evidence_route_ordinal == target_ordinal
        and all(_is_initial_skip(candidate) for candidate in state.candidates[: target_ordinal - 1])
        and all(_is_initial_pending(candidate) for candidate in state.candidates[target_ordinal:])
    )


def validate_initial_route_state(state: ModelRouteChainState) -> None:
    """只接受调用建立阶段可由规范构造器产生的零 attempt 状态。"""

    if state.attempt_lifecycle or state.selected_ordinal is not None or state.delta_fenced:
        raise BudgetOperationConflict
    reservation = state.current_reservation
    if state.active_ordinal is not None:
        if len(state.transitions) != 1:
            raise BudgetOperationConflict
        transition = state.transitions[0]
        active_candidate = state.candidates[state.active_ordinal - 1]
        if (
            state.waiting_approval_ordinal is not None
            or transition.state != "activated"
            or transition.from_ordinal is not None
            or transition.to_ordinal != state.active_ordinal
            or transition.released_token_bound != 0
            or transition.released_cost_bound is not None
            or transition.reserved_token_bound != reservation.token_bound
            or transition.reserved_cost_bound != reservation.cost_bound
            or not _initial_candidate_order_matches(state, state.active_ordinal)
            or active_candidate.reason is not None
            or not candidate_has_zero_provider_facts(active_candidate)
            or not candidate_has_no_approval_bindings(active_candidate)
        ):
            raise BudgetOperationConflict
        return
    if state.waiting_approval_ordinal is not None:
        if len(state.transitions) != 1:
            raise BudgetOperationConflict
        transition = state.transitions[0]
        waiting_candidate = state.candidates[state.waiting_approval_ordinal - 1]
        if (
            transition.state != "waiting_approval"
            or transition.from_ordinal is not None
            or transition.to_ordinal != state.waiting_approval_ordinal
            or transition.released_token_bound != 0
            or transition.released_cost_bound is not None
            or transition.reserved_token_bound != 0
            or transition.reserved_cost_bound is not None
            or not _initial_candidate_order_matches(state, state.waiting_approval_ordinal)
            or not candidate_has_zero_provider_facts(waiting_candidate)
            or waiting_candidate.approval_request_binding_digest is None
            or waiting_candidate.approval_grant_binding_digest is not None
        ):
            raise BudgetOperationConflict
        return
    if state.transitions:
        raise BudgetOperationConflict


def validate_route_state_mutation(
    previous: ModelRouteChainState,
    state: ModelRouteChainState,
    *,
    mutation: str,
) -> None:
    """只接受同一 chain 的单调 lifecycle/proof/transition 前缀扩展。"""

    if (
        previous.chain_id != state.chain_id
        or previous.candidate_count != state.candidate_count
        or previous.usage_call_id != state.usage_call_id
        or previous.operation_identity_digest != state.operation_identity_digest
        or [
            (item.ordinal, item.deployment_id, item.provider, item.model, item.route_digest)
            for item in previous.candidates
        ]
        != [
            (item.ordinal, item.deployment_id, item.provider, item.model, item.route_digest)
            for item in state.candidates
        ]
        or state.attempt_lifecycle[: len(previous.attempt_lifecycle)] != previous.attempt_lifecycle
        and mutation not in {"proof", "close", "unknown_close", "delta"}
        or state.transitions[: len(previous.transitions)] != previous.transitions
    ):
        raise BudgetOperationConflict
    if previous.delta_fenced and not state.delta_fenced:
        raise BudgetOperationConflict
    candidate_changes = [
        after.ordinal
        for before, after in zip(previous.candidates, state.candidates, strict=True)
        if before != after
    ]
    if (
        mutation in {"attempt_started", "proof", "delta"}
        and state.current_reservation != previous.current_reservation
    ):
        raise BudgetOperationConflict
    if mutation == "attempt_started":
        if (
            len(state.attempt_lifecycle) != len(previous.attempt_lifecycle) + 1
            or not _is_canonical_started_identity(state.attempt_lifecycle[-1])
            or any(
                item.lifecycle_state != "not_started_proven" for item in previous.attempt_lifecycle
            )
            or state.transitions != previous.transitions
            or candidate_changes
        ):
            raise BudgetOperationConflict
    elif mutation == "proof":
        if len(state.attempt_lifecycle) != len(previous.attempt_lifecycle):
            raise BudgetOperationConflict
        changed = [
            (before, after)
            for before, after in zip(
                previous.attempt_lifecycle, state.attempt_lifecycle, strict=True
            )
            if before != after
        ]
        if (
            len(changed) != 1
            or changed[0][0].lifecycle_state != "started"
            or changed[0][1].lifecycle_state != "not_started_proven"
            or changed[0][0] != previous.attempt_lifecycle[-1]
            or candidate_changes != [changed[0][1].candidate_ordinal]
        ):
            raise BudgetOperationConflict
    elif mutation == "transfer":
        if len(state.transitions) != len(previous.transitions) + 1 or state.transitions[
            -1
        ].state not in {"transferred", "terminated", "waiting_approval"}:
            raise BudgetOperationConflict
        transition = state.transitions[-1]
        before_reservation = previous.current_reservation
        after_reservation = state.current_reservation
        if (
            transition.released_token_bound != before_reservation.token_bound
            or transition.released_cost_bound != before_reservation.cost_bound
            or transition.reserved_token_bound != after_reservation.token_bound
            or transition.reserved_cost_bound != after_reservation.cost_bound
            or (transition.state == "transferred" and transition.to_ordinal != state.active_ordinal)
            or (
                transition.state == "waiting_approval"
                and transition.to_ordinal != state.waiting_approval_ordinal
            )
            or (
                transition.state == "terminated"
                and (state.active_ordinal is not None or state.waiting_approval_ordinal is not None)
            )
        ):
            raise BudgetOperationConflict
        if previous.active_ordinal is not None:
            source_ordinal = previous.active_ordinal
            source_attempts = [
                item for item in state.attempt_lifecycle if item.candidate_ordinal == source_ordinal
            ]
            source = state.candidates[source_ordinal - 1]
            if (
                transition.from_ordinal != source_ordinal
                or not source_attempts
                or any(item.lifecycle_state != "not_started_proven" for item in source_attempts)
                or len(source.not_started_proofs) != len(source_attempts)
            ):
                raise BudgetOperationConflict
        else:
            source_ordinal = transition.from_ordinal
            if source_ordinal is None:
                raise BudgetOperationConflict
            source = previous.candidates[source_ordinal - 1]
            if (
                source.state != "budget_ineligible"
                or source.reason != "balance"
                or source.approval_request_binding_digest is None
                or source.approval_grant_binding_digest is None
                or transition.released_token_bound != 0
                or transition.released_cost_bound is not None
            ):
                raise BudgetOperationConflict
    elif mutation == "approval":
        ordinal = previous.waiting_approval_ordinal
        transition = state.transitions[-1] if state.transitions else None
        before_candidate = None if ordinal is None else previous.candidates[ordinal - 1]
        after_candidate = None if ordinal is None else state.candidates[ordinal - 1]
        if (
            len(state.transitions) != len(previous.transitions) + 1
            or ordinal is None
            or transition is None
            or transition.state != "approved"
            or transition.from_ordinal != ordinal
            or transition.to_ordinal != ordinal
            or transition.released_token_bound != previous.current_reservation.token_bound
            or transition.released_cost_bound != previous.current_reservation.cost_bound
            or transition.reserved_token_bound != state.current_reservation.token_bound
            or transition.reserved_cost_bound != state.current_reservation.cost_bound
            or previous.active_ordinal is not None
            or previous.current_reservation.candidate_ordinal is not None
            or previous.current_reservation.token_bound != 0
            or previous.current_reservation.cost_bound is not None
            or state.waiting_approval_ordinal is not None
            or state.active_ordinal != ordinal
            or state.current_reservation.candidate_ordinal != ordinal
            or state.attempt_lifecycle != previous.attempt_lifecycle
            or state.delta_fenced != previous.delta_fenced
            or before_candidate is None
            or after_candidate is None
            or before_candidate.state != "waiting_approval"
            or after_candidate.state != "active"
            or before_candidate.approval_request_binding_digest is None
            or after_candidate.approval_request_binding_digest
            != before_candidate.approval_request_binding_digest
            or after_candidate.approval_grant_binding_digest is None
            or candidate_changes != [ordinal]
        ):
            raise BudgetOperationConflict
    elif mutation == "approval_balance":
        if (
            state.transitions != previous.transitions
            or previous.waiting_approval_ordinal is None
            or state.waiting_approval_ordinal is not None
        ):
            raise BudgetOperationConflict
        ordinal = previous.waiting_approval_ordinal
        before = previous.candidates[ordinal - 1]
        after = state.candidates[ordinal - 1]
        if (
            before.state != "waiting_approval"
            or after.state != "budget_ineligible"
            or after.reason != "balance"
            or before.approval_request_binding_digest != after.approval_request_binding_digest
            or after.approval_grant_binding_digest is None
            or state.current_reservation.token_bound != 0
            or state.current_reservation.cost_bound is not None
            or candidate_changes != [ordinal]
        ):
            raise BudgetOperationConflict
    elif mutation in {"close", "unknown_close"}:
        if len(state.attempt_lifecycle) != len(previous.attempt_lifecycle):
            raise BudgetOperationConflict
        changed = [
            (before, after)
            for before, after in zip(
                previous.attempt_lifecycle, state.attempt_lifecycle, strict=True
            )
            if before != after
        ]
        if (
            len(changed) != 1
            or changed[0][0].lifecycle_state != "started"
            or changed[0][0] != previous.attempt_lifecycle[-1]
            or changed[0][1].lifecycle_state
            != ("unknown" if mutation == "unknown_close" else "settled")
            or any(
                item.lifecycle_state != "not_started_proven"
                for item in previous.attempt_lifecycle[:-1]
            )
            or candidate_changes != [changed[0][1].candidate_ordinal]
            or state.transitions != previous.transitions
            or (
                mutation == "unknown_close"
                and state.current_reservation != previous.current_reservation
            )
        ):
            raise BudgetOperationConflict
    elif mutation == "delta":
        if (
            len(state.attempt_lifecycle) != len(previous.attempt_lifecycle)
            or previous.delta_fenced
            or not state.delta_fenced
        ):
            raise BudgetOperationConflict
        changed = [
            (before, after)
            for before, after in zip(
                previous.attempt_lifecycle, state.attempt_lifecycle, strict=True
            )
            if before != after
        ]
        if (
            len(changed) != 1
            or changed[0][0].lifecycle_state != "started"
            or changed[0][1].lifecycle_state != "started"
            or changed[0][0] != previous.attempt_lifecycle[-1]
            or not changed[0][1].delta_observed
            or candidate_changes != [changed[0][1].candidate_ordinal]
        ):
            raise BudgetOperationConflict
    else:
        raise BudgetOperationConflict
