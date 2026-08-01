"""Route-chain 初始扫描与零副作用候选状态转换。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.storage.model_route_chain_state import ModelRouteChainState


def initial_route_chain_state(
    *,
    chain: ModelRouteChainPlan,
    usage_call_id: str,
    operation_identity_digest: str,
    cost_enabled: bool,
) -> ModelRouteChainState:
    """建立首候选 active 的唯一初始 reservation 与 transition。"""

    first = chain.candidates[0]
    cost_bound = (
        float(first.reserved_cost_bound)
        if cost_enabled and first.reserved_cost_bound is not None
        else None
    )
    return ModelRouteChainState.model_validate(
        {
            "schema_version": "model-route-chain-state-v1",
            "chain_id": chain.chain_id,
            "candidate_count": chain.candidate_count,
            "usage_call_id": usage_call_id,
            "operation_identity_digest": operation_identity_digest,
            "active_ordinal": 1,
            "waiting_approval_ordinal": None,
            "selected_ordinal": None,
            "evidence_route_ordinal": 1,
            "delta_fenced": False,
            "attempt_lifecycle": [],
            "current_reservation": {
                "candidate_ordinal": 1,
                "token_bound": first.reserved_token_bound,
                "cost_bound": cost_bound,
            },
            "candidates": [
                {
                    "ordinal": item.ordinal,
                    "deployment_id": item.deployment_id,
                    "provider": item.provider,
                    "model": item.model,
                    "route_digest": item.route_digest,
                    "state": "active" if item.ordinal == 1 else "pending",
                    "side_effect_state": "not_started",
                    "reason": None,
                    "request_sent": False,
                    "http_response_observed": False,
                    "http_status": None,
                    "response_identity_observed": False,
                    "usage_observed": False,
                    "text_observed": False,
                    "delta_observed": False,
                    "completion_observed": None,
                    "not_started_proofs": [],
                    "approval_request_binding_digest": None,
                    "approval_grant_binding_digest": None,
                }
                for item in chain.candidates
            ],
            "transitions": [
                {
                    "sequence": 1,
                    "from_ordinal": None,
                    "to_ordinal": 1,
                    "state": "activated",
                    "reason": "initial",
                    "released_token_bound": 0,
                    "released_cost_bound": None,
                    "reserved_token_bound": first.reserved_token_bound,
                    "reserved_cost_bound": cost_bound,
                }
            ],
        }
    )


def initial_scanned_route_chain_state(
    *,
    chain: ModelRouteChainPlan,
    usage_call_id: str,
    operation_identity_digest: str,
    cost_enabled: bool,
    active_ordinal: int,
    skipped: Mapping[int, Literal["static_ineligible", "soft_budget", "balance"]],
) -> ModelRouteChainState:
    """初始扫描跨过零影响候选后，只以 null→首个 eligible 建立 reservation。"""

    if not 1 <= active_ordinal <= chain.candidate_count:
        raise ValueError("initial active ordinal is outside the frozen chain")
    if any(ordinal >= active_ordinal for ordinal in skipped):
        raise ValueError("initial skips must precede the active candidate")
    payload = initial_route_chain_state(
        chain=chain,
        usage_call_id=usage_call_id,
        operation_identity_digest=operation_identity_digest,
        cost_enabled=cost_enabled,
    ).to_payload()
    for ordinal, reason in skipped.items():
        candidate = payload["candidates"][ordinal - 1]
        candidate.update(
            {
                "state": (
                    "static_ineligible" if reason == "static_ineligible" else "budget_ineligible"
                ),
                "reason": reason,
            }
        )
    if active_ordinal != 1:
        payload["candidates"][0].update(
            {
                "state": (
                    "static_ineligible"
                    if skipped.get(1) == "static_ineligible"
                    else "budget_ineligible"
                ),
                "reason": skipped[1],
            }
        )
        target = chain.candidates[active_ordinal - 1]
        target_cost = (
            float(target.reserved_cost_bound)
            if cost_enabled and target.reserved_cost_bound is not None
            else None
        )
        payload["candidates"][active_ordinal - 1]["state"] = "active"
        payload["active_ordinal"] = active_ordinal
        payload["evidence_route_ordinal"] = active_ordinal
        payload["current_reservation"] = {
            "candidate_ordinal": active_ordinal,
            "token_bound": target.reserved_token_bound,
            "cost_bound": target_cost,
        }
        payload["transitions"][0].update(
            {
                "to_ordinal": active_ordinal,
                "reserved_token_bound": target.reserved_token_bound,
                "reserved_cost_bound": target_cost,
            }
        )
    return ModelRouteChainState.model_validate(payload)


def initial_waiting_route_chain_state(
    *,
    chain: ModelRouteChainPlan,
    usage_call_id: str,
    operation_identity_digest: str,
    candidate_ordinal: int,
    approval_request_binding_digest: str,
    skipped: Mapping[int, Literal["static_ineligible", "soft_budget", "balance"]] | None = None,
) -> ModelRouteChainState:
    """建立零 impact coordination carrier，并把目标候选冻结为 waiting。"""

    if not 1 <= candidate_ordinal <= chain.candidate_count:
        raise ValueError("approval candidate ordinal is outside the frozen chain")
    state = ModelRouteChainState.model_validate(
        {
            "schema_version": "model-route-chain-state-v1",
            "chain_id": chain.chain_id,
            "candidate_count": chain.candidate_count,
            "usage_call_id": usage_call_id,
            "operation_identity_digest": operation_identity_digest,
            "active_ordinal": None,
            "waiting_approval_ordinal": candidate_ordinal,
            "selected_ordinal": None,
            "evidence_route_ordinal": candidate_ordinal,
            "delta_fenced": False,
            "attempt_lifecycle": [],
            "current_reservation": {
                "candidate_ordinal": None,
                "token_bound": 0,
                "cost_bound": None,
            },
            "candidates": [
                {
                    "ordinal": item.ordinal,
                    "deployment_id": item.deployment_id,
                    "provider": item.provider,
                    "model": item.model,
                    "route_digest": item.route_digest,
                    "state": (
                        "waiting_approval" if item.ordinal == candidate_ordinal else "pending"
                    ),
                    "side_effect_state": "not_started",
                    "reason": ("approval_required" if item.ordinal == candidate_ordinal else None),
                    "request_sent": False,
                    "http_response_observed": False,
                    "http_status": None,
                    "response_identity_observed": False,
                    "usage_observed": False,
                    "text_observed": False,
                    "delta_observed": False,
                    "completion_observed": None,
                    "not_started_proofs": [],
                    "approval_request_binding_digest": (
                        approval_request_binding_digest
                        if item.ordinal == candidate_ordinal
                        else None
                    ),
                    "approval_grant_binding_digest": None,
                }
                for item in chain.candidates
            ],
            "transitions": [
                {
                    "sequence": 1,
                    "from_ordinal": None,
                    "to_ordinal": candidate_ordinal,
                    "state": "waiting_approval",
                    "reason": "approval_required",
                    "released_token_bound": 0,
                    "released_cost_bound": None,
                    "reserved_token_bound": 0,
                    "reserved_cost_bound": None,
                }
            ],
        }
    )
    return _apply_initial_skips(state, skipped or {}, target_ordinal=candidate_ordinal)


def initial_denied_route_chain_state(
    *,
    chain: ModelRouteChainPlan,
    usage_call_id: str,
    operation_identity_digest: str,
    candidate_ordinal: int,
    skipped: Mapping[int, Literal["static_ineligible", "soft_budget", "balance"]] | None = None,
) -> ModelRouteChainState:
    """以零 impact carrier 固化初始 policy deny，不伪造 reservation transition。"""

    if not 1 <= candidate_ordinal <= chain.candidate_count:
        raise ValueError("denied candidate ordinal is outside the frozen chain")
    payload = initial_waiting_route_chain_state(
        chain=chain,
        usage_call_id=usage_call_id,
        operation_identity_digest=operation_identity_digest,
        candidate_ordinal=candidate_ordinal,
        approval_request_binding_digest="0" * 64,
        skipped=skipped,
    ).to_payload()
    target = payload["candidates"][candidate_ordinal - 1]
    target.update(
        {
            "state": "denied",
            "reason": "policy_denied",
            "approval_request_binding_digest": None,
        }
    )
    payload["waiting_approval_ordinal"] = None
    payload["transitions"] = []
    return ModelRouteChainState.model_validate(payload)


def initial_exhausted_route_chain_state(
    *,
    chain: ModelRouteChainPlan,
    usage_call_id: str,
    operation_identity_digest: str,
    skipped: Mapping[int, Literal["static_ineligible", "soft_budget", "balance"]],
) -> ModelRouteChainState:
    """所有候选资格/预算不可用时保存零 reservation、空 transition 与最后 cause。"""

    if set(skipped) != set(range(1, chain.candidate_count + 1)):
        raise ValueError("initial exhaustion must classify every candidate")
    state = initial_denied_route_chain_state(
        chain=chain,
        usage_call_id=usage_call_id,
        operation_identity_digest=operation_identity_digest,
        candidate_ordinal=chain.candidate_count,
        skipped={
            ordinal: reason
            for ordinal, reason in skipped.items()
            if ordinal < chain.candidate_count
        },
    )
    payload = state.to_payload()
    last_reason = skipped[chain.candidate_count]
    payload["candidates"][chain.candidate_count - 1].update(
        {
            "state": (
                "static_ineligible" if last_reason == "static_ineligible" else "budget_ineligible"
            ),
            "reason": last_reason,
        }
    )
    payload["evidence_route_ordinal"] = chain.candidate_count
    return ModelRouteChainState.model_validate(payload)


def _apply_initial_skips(
    state: ModelRouteChainState,
    skipped: Mapping[int, Literal["static_ineligible", "soft_budget", "balance"]],
    *,
    target_ordinal: int,
) -> ModelRouteChainState:
    """把 target 前的初始零影响分类写入 state，且不改变 null source anchor。"""

    if any(ordinal >= target_ordinal for ordinal in skipped):
        raise ValueError("initial skips must precede the target candidate")
    payload = state.to_payload()
    for ordinal, reason in skipped.items():
        candidate = payload["candidates"][ordinal - 1]
        candidate.update(
            {
                "state": (
                    "static_ineligible" if reason == "static_ineligible" else "budget_ineligible"
                ),
                "reason": reason,
            }
        )
    return ModelRouteChainState.model_validate(payload)


__all__ = [
    "initial_route_chain_state",
    "initial_scanned_route_chain_state",
    "initial_waiting_route_chain_state",
    "initial_denied_route_chain_state",
    "initial_exhausted_route_chain_state",
]
