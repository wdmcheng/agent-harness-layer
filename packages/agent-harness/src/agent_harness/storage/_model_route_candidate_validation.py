"""逐候选 route-chain 状态的封闭 shape 校验。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_harness.storage.model_route_chain_state import ModelRouteCandidateState


def validate_model_route_candidate_state(
    candidate: ModelRouteCandidateState,
) -> ModelRouteCandidateState:
    """校验零影响、审批 binding 与 terminal observation 的互斥关系。"""

    from agent_harness.storage.model_route_chain_state import (
        candidate_has_no_approval_bindings,
        candidate_has_zero_provider_facts,
    )

    if candidate.state in {
        "pending",
        "static_ineligible",
        "budget_ineligible",
        "waiting_approval",
        "denied",
    } and not candidate_has_zero_provider_facts(candidate):
        raise ValueError("zero-impact candidate contains provider observations")
    if candidate.state == "pending" and (
        candidate.reason is not None or not candidate_has_no_approval_bindings(candidate)
    ):
        raise ValueError("pending candidate shape is invalid")
    if candidate.state == "static_ineligible" and (
        candidate.reason != "static_ineligible" or not candidate_has_no_approval_bindings(candidate)
    ):
        raise ValueError("static-ineligible candidate shape is invalid")
    if candidate.state == "budget_ineligible" and candidate.reason not in {
        "soft_budget",
        "balance",
    }:
        raise ValueError("budget-ineligible reason is invalid")
    if (
        candidate.state == "budget_ineligible"
        and candidate.reason == "soft_budget"
        and not candidate_has_no_approval_bindings(candidate)
    ):
        raise ValueError("soft-budget skip cannot carry approval bindings")
    if candidate.state == "waiting_approval":
        if (
            candidate.reason != "approval_required"
            or candidate.approval_request_binding_digest is None
        ):
            raise ValueError("waiting approval binding is incomplete")
        if candidate.approval_grant_binding_digest is not None:
            raise ValueError("waiting approval cannot already carry a grant")
    if candidate.state == "denied" and (
        candidate.reason != "policy_denied"
        or candidate.approval_request_binding_digest is not None
        or candidate.approval_grant_binding_digest is not None
    ):
        raise ValueError("policy-denied candidate shape is invalid")
    if (
        candidate.approval_grant_binding_digest is not None
        and candidate.approval_request_binding_digest is None
    ):
        raise ValueError("approval grant requires its request binding")
    if candidate.state == "budget_ineligible" and (
        (candidate.approval_request_binding_digest is None)
        != (candidate.approval_grant_binding_digest is None)
    ):
        raise ValueError("approved balance skip requires both approval bindings")
    if candidate.state in {"completed", "cancelled"}:
        if candidate.side_effect_state != "result_committed":
            raise ValueError("terminal result candidate must be result committed")
    elif candidate.side_effect_state == "result_committed":
        raise ValueError("only terminal result candidates may be result committed")
    if candidate.state == "cancelled" and (
        candidate.reason != "invocation_cancelled"
        or not candidate.request_sent
        or candidate.http_response_observed
        or candidate.http_status is not None
        or candidate.response_identity_observed
        or not candidate.usage_observed
        or candidate.text_observed
        or candidate.delta_observed
        or candidate.completion_observed is not False
    ):
        raise ValueError("cancelled candidate shape is invalid")
    return candidate


__all__ = ["validate_model_route_candidate_state"]
