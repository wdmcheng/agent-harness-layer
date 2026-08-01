"""Route-chain attempt 的终态关闭与首 delta 围栏转换。"""

from __future__ import annotations

from typing import Literal

from agent_harness.storage.model_route_chain_state import ModelRouteChainState


def close_route_attempt(
    state: ModelRouteChainState,
    *,
    candidate_ordinal: int,
    lifecycle_state: str,
    response_observed: bool,
    delta_observed: bool = False,
    request_sent: bool = True,
    usage_observed: bool | None = None,
    text_observed: bool | None = None,
    completion_observed: bool | None = None,
    http_status: int | None = None,
    response_identity_observed: bool | None = None,
    terminal_outcome: Literal["completed", "cancelled"] = "completed",
) -> ModelRouteChainState:
    """把当前 attempt 关闭为 completed/cancelled/unknown 的唯一耐久形状。"""

    payload = state.to_payload()
    lifecycle = payload["attempt_lifecycle"][-1]
    if (
        lifecycle["candidate_ordinal"] != candidate_ordinal
        or lifecycle["lifecycle_state"] != "started"
    ):
        raise ValueError("attempt close does not target the current started lifecycle")
    settled = lifecycle_state == "settled"
    cancelled = settled and terminal_outcome == "cancelled"
    observed_usage = settled if usage_observed is None else usage_observed
    observed_text = settled if text_observed is None else text_observed
    observed_completion = (
        (False if cancelled else True)
        if completion_observed is None and settled
        else completion_observed
    )
    observed_http_status = (
        200 if settled and response_observed and http_status is None else http_status
    )
    observed_response_identity = (
        settled and not cancelled
        if response_identity_observed is None
        else response_identity_observed
    )
    lifecycle.update(
        {
            "lifecycle_state": lifecycle_state,
            "side_effect_state": "result_committed" if settled else "unknown",
            "request_sent": lifecycle["request_sent"] or request_sent,
            "http_response_observed": lifecycle["http_response_observed"] or response_observed,
            "http_status": observed_http_status or lifecycle["http_status"],
            "response_identity_observed": (
                lifecycle["response_identity_observed"] or observed_response_identity
            ),
            "usage_observed": lifecycle["usage_observed"] or observed_usage,
            "text_observed": lifecycle["text_observed"] or observed_text,
            "delta_observed": lifecycle["delta_observed"] or delta_observed,
            "completion_observed": (
                lifecycle["completion_observed"]
                if observed_completion is None
                else observed_completion
            ),
            "not_started_proof_digest": None,
        }
    )
    candidate = payload["candidates"][candidate_ordinal - 1]
    candidate.update(
        {
            "state": terminal_outcome if settled else "unknown",
            "side_effect_state": "result_committed" if settled else "unknown",
            "reason": (
                "invocation_cancelled"
                if cancelled
                else None
                if settled
                else "provider_side_effect_unknown"
            ),
            "request_sent": candidate["request_sent"] or request_sent,
            "http_response_observed": candidate["http_response_observed"] or response_observed,
            "http_status": observed_http_status or candidate["http_status"],
            "response_identity_observed": (
                candidate["response_identity_observed"] or observed_response_identity
            ),
            "usage_observed": candidate["usage_observed"] or observed_usage,
            "text_observed": candidate["text_observed"] or observed_text,
            "delta_observed": candidate["delta_observed"] or delta_observed,
            "completion_observed": (
                candidate["completion_observed"]
                if observed_completion is None
                else observed_completion
            ),
        }
    )
    if settled:
        payload["selected_ordinal"] = None if cancelled else candidate_ordinal
        payload["active_ordinal"] = None
        payload["waiting_approval_ordinal"] = None
        payload["evidence_route_ordinal"] = candidate_ordinal
        payload["current_reservation"] = {
            "candidate_ordinal": None,
            "token_bound": 0,
            "cost_bound": None,
        }
    return ModelRouteChainState.model_validate(payload)


def mark_route_delta_observed(
    state: ModelRouteChainState,
    *,
    candidate_ordinal: int,
) -> ModelRouteChainState:
    """首个 provider delta 一经观察就耐久升起全链切换围栏。"""

    payload = state.to_payload()
    lifecycle = payload["attempt_lifecycle"][-1]
    if (
        lifecycle["candidate_ordinal"] != candidate_ordinal
        or lifecycle["lifecycle_state"] != "started"
    ):
        raise ValueError("delta fence does not target the current started lifecycle")
    lifecycle.update(
        {
            "side_effect_state": "started",
            "request_sent": True,
            "text_observed": True,
            "delta_observed": True,
        }
    )
    candidate = payload["candidates"][candidate_ordinal - 1]
    candidate.update(
        {
            "side_effect_state": "started",
            "request_sent": True,
            "text_observed": True,
            "delta_observed": True,
        }
    )
    payload["delta_fenced"] = True
    payload["evidence_route_ordinal"] = candidate_ordinal
    return ModelRouteChainState.model_validate(payload)


__all__ = ["close_route_attempt", "mark_route_delta_observed"]
