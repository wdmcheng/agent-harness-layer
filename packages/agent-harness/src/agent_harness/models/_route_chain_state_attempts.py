"""Route-chain attempt、proof、reservation transfer 与终态转换。"""

from __future__ import annotations

from typing import Literal

from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import RouteAttemptNotStartedFacts
from agent_harness.models.route_chain_identity import (
    ModelRouteAttemptIdentity,
    ModelRouteNotStartedProofIdentity,
    validate_route_identity_digest,
)
from agent_harness.storage.model_route_chain_state import ModelRouteChainState


def validate_route_chain_state_identities(
    state: ModelRouteChainState,
    *,
    chain: ModelRouteChainPlan,
) -> None:
    """按冻结chain重算全部耐久attempt/proof摘要并核对候选身份。

    DTO只验证图结构和摘要引用关系；恢复授权还必须证明摘要来自当前冻结chain
    的canonical bytes，不能让同步篡改的错误引用授权后继provider副作用。
    """

    if state.chain_id != chain.chain_id or state.candidate_count != chain.candidate_count:
        raise ValueError("route-chain state does not match its frozen plan")
    for durable, planned in zip(state.candidates, chain.candidates, strict=True):
        if (
            durable.ordinal != planned.ordinal
            or durable.deployment_id != planned.deployment_id
            or durable.provider != planned.provider
            or durable.model != planned.model
            or durable.route_digest != planned.route_digest
        ):
            raise ValueError("durable route candidate identity does not match frozen plan")

    for lifecycle in state.attempt_lifecycle:
        planned = chain.candidates[lifecycle.candidate_ordinal - 1]
        identity = ModelRouteAttemptIdentity(
            schema_version="model-route-attempt-identity-v1",
            chain_id=chain.chain_id,
            usage_call_id=state.usage_call_id,
            operation_identity_digest=state.operation_identity_digest,
            candidate_ordinal=lifecycle.candidate_ordinal,
            global_attempt=lifecycle.attempt,
            route_digest=planned.route_digest,
            endpoint_policy_digest=planned.endpoint_policy_digest,
            retry_policy_digest=planned.retry_policy_digest,
        )
        validate_route_identity_digest(identity, lifecycle.attempt_identity_digest)

    for durable in state.candidates:
        planned = chain.candidates[durable.ordinal - 1]
        for proof in durable.not_started_proofs:
            if proof.endpoint_policy_digest != planned.endpoint_policy_digest:
                raise ValueError("not-started proof endpoint policy does not match frozen plan")
            if proof.reason == "trusted_business_not_started" and (
                proof.classifier_ref != planned.route.completion_classifier_ref
                or proof.classifier_version != planned.route.completion_classifier_version
                or proof.http_status not in planned.route.cross_provider_failover_http_statuses
            ):
                raise ValueError("trusted not-started proof does not match frozen classifier")
            identity = ModelRouteNotStartedProofIdentity(
                schema_version="model-route-not-started-proof-v1",
                chain_id=chain.chain_id,
                candidate_ordinal=durable.ordinal,
                global_attempt=proof.attempt,
                reason=proof.reason,
                attempt_side_effect_state=proof.side_effect_state,
                request_sent=proof.request_sent,
                http_response_observed=proof.http_response_observed,
                http_status=proof.http_status,
                response_identity_observed=proof.response_identity_observed,
                usage_observed=proof.usage_observed,
                text_observed=proof.text_observed,
                delta_observed=proof.delta_observed,
                completion_observed=proof.completion_observed,
                endpoint_policy_digest=proof.endpoint_policy_digest,
                classifier_ref=proof.classifier_ref,
                classifier_version=proof.classifier_version,
            )
            validate_route_identity_digest(identity, proof.proof_digest)


def append_route_attempt_started(
    state: ModelRouteChainState,
    *,
    chain: ModelRouteChainPlan,
    candidate_ordinal: int,
) -> ModelRouteChainState:
    """追加全局连续 started identity；identity 一经落库不可被重写。"""

    candidate = chain.candidates[candidate_ordinal - 1]
    attempt = len(state.attempt_lifecycle) + 1
    identity = ModelRouteAttemptIdentity.model_validate(
        {
            "schema_version": "model-route-attempt-identity-v1",
            "chain_id": chain.chain_id,
            "usage_call_id": state.usage_call_id,
            "operation_identity_digest": state.operation_identity_digest,
            "candidate_ordinal": candidate_ordinal,
            "global_attempt": attempt,
            "route_digest": candidate.route_digest,
            "endpoint_policy_digest": candidate.endpoint_policy_digest,
            "retry_policy_digest": candidate.retry_policy_digest,
        }
    )
    payload = state.to_payload()
    payload["attempt_lifecycle"].append(
        {
            "attempt": attempt,
            "candidate_ordinal": candidate_ordinal,
            "attempt_identity_digest": identity.digest(),
            "lifecycle_state": "started",
            "side_effect_state": "not_started",
            "request_sent": False,
            "http_response_observed": False,
            "http_status": None,
            "response_identity_observed": False,
            "usage_observed": False,
            "text_observed": False,
            "delta_observed": False,
            "completion_observed": None,
            "not_started_proof_digest": None,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def prove_route_attempt_not_started(
    state: ModelRouteChainState,
    *,
    candidate_ordinal: int,
    facts: RouteAttemptNotStartedFacts,
) -> ModelRouteChainState:
    """用端点绑定事实原子关闭当前 lifecycle，并追加不可覆盖 proof。"""

    payload = state.to_payload()
    lifecycle = payload["attempt_lifecycle"][-1]
    attempt = lifecycle["attempt"]
    if (
        lifecycle["candidate_ordinal"] != candidate_ordinal
        or lifecycle["lifecycle_state"] != "started"
    ):
        raise ValueError("not-started proof does not close the current attempt")
    proof_identity = ModelRouteNotStartedProofIdentity.model_validate(
        {
            "schema_version": "model-route-not-started-proof-v1",
            "chain_id": state.chain_id,
            "candidate_ordinal": candidate_ordinal,
            "global_attempt": attempt,
            "reason": facts.not_started_reason,
            "attempt_side_effect_state": facts.side_effect_state,
            "request_sent": facts.request_sent,
            "http_response_observed": facts.http_response_observed,
            "http_status": facts.http_status,
            "response_identity_observed": facts.response_identity_observed,
            "usage_observed": facts.usage_observed,
            "text_observed": facts.text_observed,
            "delta_observed": facts.delta_observed,
            "completion_observed": facts.completion_observed,
            "endpoint_policy_digest": facts.endpoint_policy_digest,
            "classifier_ref": facts.classifier_ref,
            "classifier_version": facts.classifier_version,
        }
    )
    proof_digest = proof_identity.digest()
    proof = {
        "attempt": attempt,
        "reason": facts.not_started_reason,
        "side_effect_state": facts.side_effect_state,
        "request_sent": facts.request_sent,
        "http_response_observed": facts.http_response_observed,
        "http_status": facts.http_status,
        "response_identity_observed": facts.response_identity_observed,
        "usage_observed": facts.usage_observed,
        "text_observed": facts.text_observed,
        "delta_observed": facts.delta_observed,
        "completion_observed": facts.completion_observed,
        "endpoint_policy_digest": facts.endpoint_policy_digest,
        "classifier_ref": facts.classifier_ref,
        "classifier_version": facts.classifier_version,
        "proof_digest": proof_digest,
    }
    lifecycle.update(
        {
            "lifecycle_state": "not_started_proven",
            "side_effect_state": facts.side_effect_state,
            "request_sent": facts.request_sent,
            "http_response_observed": facts.http_response_observed,
            "http_status": facts.http_status,
            "response_identity_observed": facts.response_identity_observed,
            "usage_observed": facts.usage_observed,
            "text_observed": facts.text_observed,
            "delta_observed": facts.delta_observed,
            "completion_observed": facts.completion_observed,
            "not_started_proof_digest": proof_digest,
        }
    )
    candidate = payload["candidates"][candidate_ordinal - 1]
    candidate["not_started_proofs"].append(proof)
    candidate.update(
        {
            # 当前 reservation 尚未转移时仍是 active；最后一次 proof 与 transfer
            # 在 repository owner lock 内闭合后才成为 `not_started`。
            "state": "active",
            "side_effect_state": (
                "started"
                if facts.side_effect_state == "started"
                or candidate["side_effect_state"] == "started"
                else "not_started"
            ),
            "reason": facts.not_started_reason,
            "request_sent": candidate["request_sent"] or facts.request_sent,
            "http_response_observed": (
                candidate["http_response_observed"] or facts.http_response_observed
            ),
            "http_status": facts.http_status or candidate["http_status"],
            "response_identity_observed": (
                candidate["response_identity_observed"] or facts.response_identity_observed
            ),
            "usage_observed": candidate["usage_observed"] or facts.usage_observed,
            "text_observed": candidate["text_observed"] or facts.text_observed,
            "delta_observed": candidate["delta_observed"] or facts.delta_observed,
            "completion_observed": (
                facts.completion_observed
                if facts.completion_observed is not None
                else candidate["completion_observed"]
            ),
        }
    )
    return ModelRouteChainState.model_validate(payload)


def transfer_route_reservation(
    state: ModelRouteChainState,
    *,
    chain: ModelRouteChainPlan,
    to_ordinal: int | None,
    reason: str,
    cost_enabled: bool,
) -> ModelRouteChainState:
    """从当前 source anchor 原子转移到后继，或以零 reservation 安全耗尽。"""

    payload = state.to_payload()
    from_ordinal = state.active_ordinal
    if from_ordinal is None:
        raise ValueError("route transfer requires an active source")
    old = state.current_reservation
    payload["candidates"][from_ordinal - 1]["state"] = "not_started"
    if to_ordinal is None:
        payload["active_ordinal"] = None
        payload["current_reservation"] = {
            "candidate_ordinal": None,
            "token_bound": 0,
            "cost_bound": None,
        }
        transition_state = "terminated"
        transition_reason = "route_exhausted"
        reserved_tokens = 0
        reserved_cost = None
    else:
        target = chain.candidates[to_ordinal - 1]
        payload["active_ordinal"] = to_ordinal
        payload["evidence_route_ordinal"] = to_ordinal
        payload["candidates"][to_ordinal - 1]["state"] = "active"
        payload["current_reservation"] = {
            "candidate_ordinal": to_ordinal,
            "token_bound": target.reserved_token_bound,
            "cost_bound": (
                float(target.reserved_cost_bound)
                if cost_enabled and target.reserved_cost_bound is not None
                else None
            ),
        }
        transition_state = "transferred"
        transition_reason = reason
        reserved_tokens = target.reserved_token_bound
        reserved_cost = payload["current_reservation"]["cost_bound"]
    payload["transitions"].append(
        {
            "sequence": len(state.transitions) + 1,
            "from_ordinal": from_ordinal,
            "to_ordinal": to_ordinal,
            "state": transition_state,
            "reason": transition_reason,
            "released_token_bound": old.token_bound,
            "released_cost_bound": old.cost_bound,
            "reserved_token_bound": reserved_tokens,
            "reserved_cost_bound": reserved_cost,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def wait_for_route_approval(
    state: ModelRouteChainState,
    *,
    target_ordinal: int,
    approval_request_binding_digest: str,
) -> ModelRouteChainState:
    """原子释放已证明未开始的 source，并让唯一后继进入零 impact waiting。"""

    source_ordinal = state.active_ordinal
    if source_ordinal is None or target_ordinal <= source_ordinal:
        raise ValueError("successor approval requires an active earlier source")
    payload = state.to_payload()
    source = payload["candidates"][source_ordinal - 1]
    target = payload["candidates"][target_ordinal - 1]
    if source["state"] != "active" or target["state"] != "pending":
        raise ValueError("successor approval candidates are not in canonical states")
    source["state"] = "not_started"
    target.update(
        {
            "state": "waiting_approval",
            "reason": "approval_required",
            "approval_request_binding_digest": approval_request_binding_digest,
        }
    )
    old = state.current_reservation
    payload["active_ordinal"] = None
    payload["waiting_approval_ordinal"] = target_ordinal
    payload["evidence_route_ordinal"] = target_ordinal
    payload["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    payload["transitions"].append(
        {
            "sequence": len(state.transitions) + 1,
            "from_ordinal": source_ordinal,
            "to_ordinal": target_ordinal,
            "state": "waiting_approval",
            "reason": "approval_required",
            "released_token_bound": old.token_bound,
            "released_cost_bound": old.cost_bound,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def terminate_route_policy_denied(
    state: ModelRouteChainState,
    *,
    target_ordinal: int,
) -> ModelRouteChainState:
    """释放已安全收敛的 source，并以零 charge 终止在后继 policy deny。"""

    source_ordinal = state.active_ordinal
    if source_ordinal is None or target_ordinal <= source_ordinal:
        raise ValueError("successor policy deny requires an active earlier source")
    payload = state.to_payload()
    source = payload["candidates"][source_ordinal - 1]
    target = payload["candidates"][target_ordinal - 1]
    if source["state"] != "active" or target["state"] != "pending":
        raise ValueError("policy-denied candidates are not in canonical states")
    source["state"] = "not_started"
    target.update({"state": "denied", "reason": "policy_denied"})
    old = state.current_reservation
    payload["active_ordinal"] = None
    payload["evidence_route_ordinal"] = target_ordinal
    payload["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    payload["transitions"].append(
        {
            "sequence": len(state.transitions) + 1,
            "from_ordinal": source_ordinal,
            "to_ordinal": None,
            "state": "terminated",
            "reason": "policy_denied",
            "released_token_bound": old.token_bound,
            "released_cost_bound": old.cost_bound,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        }
    )
    return ModelRouteChainState.model_validate(payload)


def mark_route_budget_ineligible(
    state: ModelRouteChainState,
    *,
    candidate_ordinal: int,
    reason: Literal["soft_budget", "balance"] = "balance",
) -> ModelRouteChainState:
    """把尚未触发副作用的普通候选锁定为预算 skip，不追加 transition。"""

    if not 1 <= candidate_ordinal <= state.candidate_count:
        raise ValueError("budget candidate ordinal is outside the frozen chain")
    payload = state.to_payload()
    target = payload["candidates"][candidate_ordinal - 1]
    if target["state"] != "pending":
        raise ValueError("only a pending route may become budget ineligible")
    target.update({"state": "budget_ineligible", "reason": reason})
    payload["evidence_route_ordinal"] = candidate_ordinal
    return ModelRouteChainState.model_validate(payload)


def mark_route_static_ineligible(
    state: ModelRouteChainState,
    *,
    candidate_ordinal: int,
) -> ModelRouteChainState:
    """把动态 hard eligibility 失败的 pending 候选锁定为零副作用静态 skip。"""

    if not 1 <= candidate_ordinal <= state.candidate_count:
        raise ValueError("static candidate ordinal is outside the frozen chain")
    payload = state.to_payload()
    target = payload["candidates"][candidate_ordinal - 1]
    if target["state"] != "pending":
        raise ValueError("only a pending route may become static ineligible")
    target.update({"state": "static_ineligible", "reason": "static_ineligible"})
    payload["evidence_route_ordinal"] = candidate_ordinal
    return ModelRouteChainState.model_validate(payload)


__all__ = [
    "append_route_attempt_started",
    "prove_route_attempt_not_started",
    "transfer_route_reservation",
    "wait_for_route_approval",
    "terminate_route_policy_denied",
    "mark_route_budget_ineligible",
    "mark_route_static_ineligible",
]
