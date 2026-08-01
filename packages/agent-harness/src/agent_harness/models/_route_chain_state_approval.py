"""Route-chain 审批激活、余额锚点与后继状态转换。"""

from __future__ import annotations

from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.storage.model_route_chain_state import ModelRouteChainState


def activate_approved_route(
    state: ModelRouteChainState,
    *,
    chain: ModelRouteChainPlan,
    approval_grant_binding_digest: str,
    cost_enabled: bool,
) -> ModelRouteChainState:
    """把同一 waiting ordinal 直接激活，只追加 canonical approved transition。"""

    ordinal = state.waiting_approval_ordinal
    if ordinal is None or state.active_ordinal is not None:
        raise ValueError("route chain is not waiting for approval")
    payload = state.to_payload()
    candidate = chain.candidates[ordinal - 1]
    target = payload["candidates"][ordinal - 1]
    if target["state"] != "waiting_approval" or not target["approval_request_binding_digest"]:
        raise ValueError("approval waiting binding is incomplete")
    cost_bound = (
        float(candidate.reserved_cost_bound)
        if cost_enabled and candidate.reserved_cost_bound is not None
        else None
    )
    target.update(
        {
            "state": "active",
            "reason": None,
            "approval_grant_binding_digest": approval_grant_binding_digest,
        }
    )
    payload["waiting_approval_ordinal"] = None
    payload["active_ordinal"] = ordinal
    payload["current_reservation"] = {
        "candidate_ordinal": ordinal,
        "token_bound": candidate.reserved_token_bound,
        "cost_bound": cost_bound,
    }
    payload["transitions"].append(
        {
            "sequence": len(state.transitions) + 1,
            "from_ordinal": ordinal,
            "to_ordinal": ordinal,
            "state": "approved",
            "reason": "approval_granted",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": candidate.reserved_token_bound,
            "reserved_cost_bound": cost_bound,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def mark_approved_route_balance_ineligible(
    state: ModelRouteChainState,
    *,
    approval_grant_binding_digest: str,
) -> ModelRouteChainState:
    """获批目标余额不足时保留双 binding 与零 impact，不伪造 approved transition。"""

    ordinal = state.waiting_approval_ordinal
    if ordinal is None or state.active_ordinal is not None:
        raise ValueError("route chain is not waiting for approval")
    payload = state.to_payload()
    target = payload["candidates"][ordinal - 1]
    if target["state"] != "waiting_approval" or not target["approval_request_binding_digest"]:
        raise ValueError("approval waiting binding is incomplete")
    target.update(
        {
            "state": "budget_ineligible",
            "reason": "balance",
            "approval_grant_binding_digest": approval_grant_binding_digest,
        }
    )
    payload["waiting_approval_ordinal"] = None
    payload["evidence_route_ordinal"] = ordinal
    return ModelRouteChainState.model_validate(payload)


def advance_from_approved_balance_anchor(
    state: ModelRouteChainState,
    *,
    chain: ModelRouteChainPlan | None,
    anchor_ordinal: int,
    target_ordinal: int | None,
    cost_enabled: bool,
) -> ModelRouteChainState:
    """从获批 balance anchor 跨普通 skip 建立后继 reservation，或零释放耗尽。"""

    if state.active_ordinal is not None or state.waiting_approval_ordinal is not None:
        raise ValueError("approved balance advance requires a zero-impact chain")
    anchor = state.candidates[anchor_ordinal - 1]
    if (
        anchor.state != "budget_ineligible"
        or anchor.reason != "balance"
        or anchor.approval_request_binding_digest is None
        or anchor.approval_grant_binding_digest is None
    ):
        raise ValueError("approved balance source anchor is invalid")
    payload = state.to_payload()
    if target_ordinal is None:
        transition_state = "terminated"
        transition_reason = "route_exhausted"
        reserved_tokens = 0
        reserved_cost = None
    else:
        if target_ordinal <= anchor_ordinal:
            raise ValueError("approved balance target must follow its source anchor")
        if chain is None:
            raise ValueError("approved balance activation requires its frozen chain")
        target = chain.candidates[target_ordinal - 1]
        target_state = payload["candidates"][target_ordinal - 1]
        if target_state["state"] != "pending":
            raise ValueError("approved balance target is not pending")
        reserved_cost = (
            float(target.reserved_cost_bound)
            if cost_enabled and target.reserved_cost_bound is not None
            else None
        )
        reserved_tokens = target.reserved_token_bound
        target_state["state"] = "active"
        payload["active_ordinal"] = target_ordinal
        payload["evidence_route_ordinal"] = target_ordinal
        payload["current_reservation"] = {
            "candidate_ordinal": target_ordinal,
            "token_bound": reserved_tokens,
            "cost_bound": reserved_cost,
        }
        transition_state = "transferred"
        transition_reason = "balance"
    payload["transitions"].append(
        {
            "sequence": len(state.transitions) + 1,
            "from_ordinal": anchor_ordinal,
            "to_ordinal": target_ordinal,
            "state": transition_state,
            "reason": transition_reason,
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": reserved_tokens,
            "reserved_cost_bound": reserved_cost,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def wait_after_approved_balance_anchor(
    state: ModelRouteChainState,
    *,
    anchor_ordinal: int,
    target_ordinal: int,
    approval_request_binding_digest: str,
) -> ModelRouteChainState:
    """从获批 balance anchor 进入后继独立 approval，保持两次 grant 完全隔离。"""

    if state.active_ordinal is not None or state.waiting_approval_ordinal is not None:
        raise ValueError("approved balance waiting requires a zero-impact chain")
    anchor = state.candidates[anchor_ordinal - 1]
    if (
        anchor.state != "budget_ineligible"
        or anchor.reason != "balance"
        or anchor.approval_request_binding_digest is None
        or anchor.approval_grant_binding_digest is None
    ):
        raise ValueError("approved balance source anchor is invalid")
    payload = state.to_payload()
    target = payload["candidates"][target_ordinal - 1]
    if target["state"] != "pending" or target_ordinal <= anchor_ordinal:
        raise ValueError("approved balance approval target is invalid")
    target.update(
        {
            "state": "waiting_approval",
            "reason": "approval_required",
            "approval_request_binding_digest": approval_request_binding_digest,
        }
    )
    payload["waiting_approval_ordinal"] = target_ordinal
    payload["evidence_route_ordinal"] = target_ordinal
    payload["transitions"].append(
        {
            "sequence": len(state.transitions) + 1,
            "from_ordinal": anchor_ordinal,
            "to_ordinal": target_ordinal,
            "state": "waiting_approval",
            "reason": "approval_required",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def deny_after_approved_balance_anchor(
    state: ModelRouteChainState,
    *,
    anchor_ordinal: int,
    target_ordinal: int,
) -> ModelRouteChainState:
    """后继重新授权为 deny 时，从零 impact anchor 直接形成 policy terminal。"""

    payload = advance_from_approved_balance_anchor(
        state,
        chain=None,
        anchor_ordinal=anchor_ordinal,
        target_ordinal=None,
        cost_enabled=False,
    ).to_payload()
    target = payload["candidates"][target_ordinal - 1]
    if target["state"] != "pending" or target_ordinal <= anchor_ordinal:
        raise ValueError("approved balance deny target is invalid")
    target.update({"state": "denied", "reason": "policy_denied"})
    payload["evidence_route_ordinal"] = target_ordinal
    payload["transitions"][-1].update({"reason": "policy_denied", "from_ordinal": anchor_ordinal})
    return ModelRouteChainState.model_validate(payload)


__all__ = [
    "activate_approved_route",
    "mark_approved_route_balance_ineligible",
    "advance_from_approved_balance_anchor",
    "wait_after_approved_balance_anchor",
    "deny_after_approved_balance_anchor",
]
