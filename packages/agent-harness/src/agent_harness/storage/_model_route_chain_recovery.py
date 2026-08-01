"""显式模型 route chain 的恢复授权 oracle。"""

from __future__ import annotations

from agent_harness.storage.model_route_chain_state import (
    ModelRouteCandidateState,
    ModelRouteChainState,
    ModelRouteTransition,
    candidate_has_no_approval_bindings,
    candidate_has_zero_provider_facts,
)


def _candidate_is_proven_not_started(
    candidate: ModelRouteCandidateState,
    *,
    expected_reason: str | None = None,
) -> bool:
    """可信运行后source必须由末条proof解释聚合终态。"""

    if candidate.state != "not_started" or not candidate.not_started_proofs:
        return False
    last_reason = candidate.not_started_proofs[-1].reason
    return candidate.reason == last_reason and (
        expected_reason is None or expected_reason == last_reason
    )


def _candidate_has_approved_bindings(candidate: ModelRouteCandidateState) -> bool:
    """审批完成的候选必须同时保留request与grant摘要。"""

    return (
        candidate.approval_request_binding_digest is not None
        and candidate.approval_grant_binding_digest is not None
    )


def route_chain_can_start_active_candidate(state: ModelRouteChainState) -> bool:
    """只在完整耐久历史证明当前active尚可创建新attempt时授权恢复。"""

    active_ordinal = state.active_ordinal
    if (
        active_ordinal is None
        or state.waiting_approval_ordinal is not None
        or state.selected_ordinal is not None
        or state.delta_fenced
        or state.current_reservation.candidate_ordinal != active_ordinal
        or state.candidates[active_ordinal - 1].state != "active"
        or not state.transitions
    ):
        return False

    transition_anchor: int | None = None
    terminated = False
    previous_transition: ModelRouteTransition | None = None
    approved_ordinals: set[int] = set()
    balance_anchors: set[int] = set()
    for index, item in enumerate(state.transitions):
        if (
            terminated
            or item.from_ordinal != transition_anchor
            or (
                item.from_ordinal is not None
                and item.to_ordinal is not None
                and item.to_ordinal < item.from_ordinal
            )
            or (
                item.state == "waiting_approval"
                and item.from_ordinal is not None
                and item.to_ordinal is not None
                and item.to_ordinal <= item.from_ordinal
            )
        ):
            return False

        if item.state == "approved":
            approved_ordinal = item.to_ordinal
            if (
                approved_ordinal is None
                or approved_ordinal in approved_ordinals
                or previous_transition is None
                or previous_transition.state != "waiting_approval"
                or previous_transition.to_ordinal != approved_ordinal
                or not _candidate_has_approved_bindings(state.candidates[approved_ordinal - 1])
            ):
                return False
            approved_ordinals.add(approved_ordinal)

        if item.state == "waiting_approval":
            waiting_ordinal = item.to_ordinal
            if waiting_ordinal is None:
                return False
            waiting_candidate = state.candidates[waiting_ordinal - 1]
            if waiting_candidate.approval_request_binding_digest is None:
                return False
            next_transition = (
                state.transitions[index + 1] if index + 1 < len(state.transitions) else None
            )
            if not (
                next_transition is not None
                and next_transition.state == "approved"
                and next_transition.to_ordinal == waiting_ordinal
            ):
                if not (
                    waiting_candidate.state == "budget_ineligible"
                    and waiting_candidate.reason == "balance"
                    and _candidate_has_approved_bindings(waiting_candidate)
                ):
                    return False
                balance_anchors.add(waiting_ordinal)

        source_ordinal = item.from_ordinal
        if source_ordinal is not None and item.state in {
            "transferred",
            "waiting_approval",
            "terminated",
        }:
            source = state.candidates[source_ordinal - 1]
            proven_source = _candidate_is_proven_not_started(
                source,
                expected_reason=(
                    item.reason
                    if item.state == "transferred"
                    and item.reason in {"client_not_started", "trusted_business_not_started"}
                    else None
                ),
            )
            if proven_source and (
                source.approval_request_binding_digest is not None
                or source.approval_grant_binding_digest is not None
            ):
                proven_source = (
                    _candidate_has_approved_bindings(source) and source_ordinal in approved_ordinals
                )
            balance_source = (
                source_ordinal in balance_anchors
                and source.state == "budget_ineligible"
                and source.reason == "balance"
                and _candidate_has_approved_bindings(source)
            )
            if item.state == "transferred":
                if item.reason == "balance":
                    if not balance_source:
                        return False
                elif not proven_source:
                    return False
            elif not (proven_source or balance_source):
                return False

        terminated = item.state == "terminated"
        transition_anchor = item.to_ordinal
        previous_transition = item

    transition = state.transitions[-1]
    active_candidate = state.candidates[active_ordinal - 1]
    active_lifecycles = tuple(
        item for item in state.attempt_lifecycle if item.candidate_ordinal == active_ordinal
    )
    if (
        transition.state not in {"activated", "transferred", "approved"}
        or transition.to_ordinal != active_ordinal
        or transition.reserved_token_bound != state.current_reservation.token_bound
        or transition.reserved_cost_bound != state.current_reservation.cost_bound
        or any(
            candidate.state not in {"static_ineligible", "budget_ineligible", "not_started"}
            for candidate in state.candidates[: active_ordinal - 1]
        )
        or any(candidate.state != "pending" for candidate in state.candidates[active_ordinal:])
        or any(
            not candidate_has_zero_provider_facts(candidate)
            or not candidate_has_no_approval_bindings(candidate)
            for candidate in state.candidates[active_ordinal:]
        )
        or (not active_lifecycles and not candidate_has_zero_provider_facts(active_candidate))
        or any(
            candidate.state in {"static_ineligible", "budget_ineligible"}
            and not candidate_has_zero_provider_facts(candidate)
            for candidate in state.candidates[: active_ordinal - 1]
        )
        or any(
            candidate.state == "budget_ineligible"
            and not candidate_has_no_approval_bindings(candidate)
            and candidate.ordinal not in balance_anchors
            for candidate in state.candidates[: active_ordinal - 1]
        )
        or any(
            candidate.state == "not_started"
            and not any(
                item.from_ordinal == candidate.ordinal
                and item.state in {"transferred", "waiting_approval"}
                for item in state.transitions
            )
            for candidate in state.candidates[: active_ordinal - 1]
        )
        or (
            transition.state == "activated"
            and (
                len(state.transitions) != 1
                or any(
                    lifecycle.candidate_ordinal != active_ordinal
                    for lifecycle in state.attempt_lifecycle
                )
            )
        )
        or (
            transition.state == "approved"
            and not _candidate_has_approved_bindings(active_candidate)
        )
        or (
            transition.state != "approved"
            and (
                active_candidate.approval_request_binding_digest is not None
                or active_candidate.approval_grant_binding_digest is not None
            )
        )
    ):
        return False

    proof_digests = {
        (candidate.ordinal, proof.attempt): proof.proof_digest
        for candidate in state.candidates
        for proof in candidate.not_started_proofs
    }
    return all(
        lifecycle.candidate_ordinal <= active_ordinal
        and lifecycle.lifecycle_state == "not_started_proven"
        and lifecycle.not_started_proof_digest
        == proof_digests.get((lifecycle.candidate_ordinal, lifecycle.attempt))
        for lifecycle in state.attempt_lifecycle
    )


__all__ = ["route_chain_can_start_active_candidate"]
