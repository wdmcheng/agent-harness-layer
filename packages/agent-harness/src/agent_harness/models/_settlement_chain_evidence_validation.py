"""Route-chain settlement evidence 的逐 attempt 身份与 proof 交叉校验。"""

from __future__ import annotations

from typing import cast

from agent_harness.models._settlement_evidence_models import (
    CHAINATTEMPT_FIELDS,
    SettlementChainAttemptEvidence,
    SettlementRouteEvidence,
)
from agent_harness.models.route_chain_identity import (
    ModelRouteAttemptIdentity,
    ModelRouteChainIdentity,
    ModelRouteNotStartedProofIdentity,
)
from agent_harness.models.usage import ModelUsageEvidence, UsageInvocationReplayError
from agent_harness.storage.model_route_chain_state import ModelRouteChainState


def settlement_replay_error(state: str) -> UsageInvocationReplayError:
    """把任何不可信嵌套值统一收敛为稳定恢复错误。"""

    return UsageInvocationReplayError(state)


def _chain_route_matches_candidate(
    route: SettlementRouteEvidence,
    candidate: object,
    *,
    provider: str,
    model: str,
) -> bool:
    """公开 route 必须命中 evidence ordinal 的 frozen candidate identity。"""

    from agent_harness.models.route_chain_identity import ModelRouteCandidateIdentity

    return (
        isinstance(candidate, ModelRouteCandidateIdentity)
        and route.deployment_id == candidate.deployment_id
        and route.provider == candidate.provider == provider
        and route.model == candidate.model == model
        and route.endpoint_policy_digest == candidate.endpoint_policy_digest
        and route.model_catalog_digest == candidate.model_catalog_digest
        and route.model_catalog_ref == candidate.model_catalog_ref
        and route.model_catalog_version == candidate.model_catalog_version
        and route.reserved_token_bound == candidate.reserved_token_bound
        and route.reserved_cost_bound == candidate.reserved_cost_bound
    )


def validate_chain_attempts(
    *,
    evidence: ModelUsageEvidence,
    started: ModelUsageEvidence,
    raw_attempts: list[object],
    state_name: str,
) -> tuple[SettlementRouteEvidence, list[SettlementChainAttemptEvidence]]:
    """逐值交叉验证 chain identity/state、attempt lifecycle、proof 与 final route。"""

    started_chain = started.decision.get("route_chain")
    final_chain = evidence.decision.get("route_chain")
    if not isinstance(started_chain, dict) or not isinstance(final_chain, dict):
        raise settlement_replay_error(state_name)
    started_chain_payload = cast(dict[str, object], started_chain)
    final_chain_payload = cast(dict[str, object], final_chain)
    if set(started_chain_payload) != {"schema_version", "identity", "state"} or set(
        final_chain_payload
    ) != {
        "schema_version",
        "identity",
        "state",
    }:
        raise settlement_replay_error(state_name)
    if (
        started_chain_payload["schema_version"] != "model-route-chain-evidence-v1"
        or final_chain_payload["schema_version"] != "model-route-chain-evidence-v1"
        or started_chain_payload["identity"] != final_chain_payload["identity"]
    ):
        raise settlement_replay_error(state_name)
    try:
        identity = ModelRouteChainIdentity.model_validate(final_chain_payload["identity"])
        started_state = ModelRouteChainState.model_validate(started_chain_payload["state"])
        final_state = ModelRouteChainState.model_validate(final_chain_payload["state"])
        started_route = SettlementRouteEvidence.model_validate(started.decision.get("route"))
        final_route = SettlementRouteEvidence.model_validate(evidence.decision.get("route"))
    except (ValueError, TypeError):
        raise settlement_replay_error(state_name) from None
    if (
        started_state.chain_id != identity.chain_id
        or final_state.chain_id != identity.chain_id
        or started_state.usage_call_id != final_state.usage_call_id
        or started_state.operation_identity_digest != final_state.operation_identity_digest
        or started_state.candidate_count != identity.candidate_count
        or final_state.candidate_count != identity.candidate_count
    ):
        raise settlement_replay_error(state_name)
    immutable = [
        (item.ordinal, item.deployment_id, item.provider, item.model, item.route_digest)
        for item in identity.candidates
    ]
    for chain_state in (started_state, final_state):
        if immutable != [
            (item.ordinal, item.deployment_id, item.provider, item.model, item.route_digest)
            for item in chain_state.candidates
        ]:
            raise settlement_replay_error(state_name)
    started_candidate = identity.candidates[started_state.evidence_route_ordinal - 1]
    final_candidate = identity.candidates[final_state.evidence_route_ordinal - 1]
    if not _chain_route_matches_candidate(
        started_route,
        started_candidate,
        provider=started.provider,
        model=started.model,
    ) or not _chain_route_matches_candidate(
        final_route,
        final_candidate,
        provider=evidence.provider,
        model=evidence.model,
    ):
        raise settlement_replay_error(state_name)
    if final_state.selected_ordinal is not None and (
        final_state.selected_ordinal != final_state.evidence_route_ordinal
    ):
        raise settlement_replay_error(state_name)
    if len(raw_attempts) != len(final_state.attempt_lifecycle):
        raise settlement_replay_error(state_name)

    proofs = {
        proof.attempt: (candidate.ordinal, proof)
        for candidate in final_state.candidates
        for proof in candidate.not_started_proofs
    }
    attempts: list[SettlementChainAttemptEvidence] = []
    for expected, raw_attempt in enumerate(raw_attempts, start=1):
        if not isinstance(raw_attempt, dict):
            raise settlement_replay_error(state_name)
        raw_attempt_payload = cast(dict[str, object], raw_attempt)
        if set(raw_attempt_payload) != CHAINATTEMPT_FIELDS:
            raise settlement_replay_error(state_name)
        try:
            attempt = SettlementChainAttemptEvidence.model_validate(raw_attempt_payload)
        except (ValueError, TypeError):
            raise settlement_replay_error(state_name) from None
        lifecycle = final_state.attempt_lifecycle[expected - 1]
        candidate = identity.candidates[lifecycle.candidate_ordinal - 1]
        proof_entry = proofs.get(expected)
        proof = None if proof_entry is None else proof_entry[1]
        expected_side_effect = (
            "started"
            if lifecycle.side_effect_state == "result_committed"
            else lifecycle.side_effect_state
        )
        if (
            attempt.attempt != expected
            or attempt.candidate_ordinal != lifecycle.candidate_ordinal
            or attempt.deployment_id != candidate.deployment_id
            or attempt.provider != candidate.provider
            or attempt.model != candidate.model
            or attempt.side_effect_state != expected_side_effect
            or attempt.request_sent != lifecycle.request_sent
            or attempt.http_response_observed != lifecycle.http_response_observed
            or attempt.http_status != lifecycle.http_status
            or attempt.response_identity_observed != lifecycle.response_identity_observed
            or attempt.usage_observed != lifecycle.usage_observed
            or attempt.text_observed != lifecycle.text_observed
            or attempt.delta_observed != lifecycle.delta_observed
            or attempt.completion_observed != lifecycle.completion_observed
            or attempt.endpoint_policy_digest != candidate.endpoint_policy_digest
            or attempt.not_started_reason != (None if proof is None else proof.reason)
            or attempt.not_started_proof_digest != (None if proof is None else proof.proof_digest)
            or attempt.classifier_ref != (None if proof is None else proof.classifier_ref)
            or attempt.classifier_version != (None if proof is None else proof.classifier_version)
        ):
            raise settlement_replay_error(state_name)
        try:
            attempt_identity = ModelRouteAttemptIdentity(
                schema_version="model-route-attempt-identity-v1",
                chain_id=identity.chain_id,
                usage_call_id=final_state.usage_call_id,
                operation_identity_digest=final_state.operation_identity_digest,
                candidate_ordinal=lifecycle.candidate_ordinal,
                global_attempt=expected,
                route_digest=candidate.route_digest,
                endpoint_policy_digest=candidate.endpoint_policy_digest,
                retry_policy_digest=candidate.retry_policy_digest,
            )
            if attempt_identity.digest() != lifecycle.attempt_identity_digest:
                raise ValueError("attempt identity mismatch")
            if proof is not None:
                proof_identity = ModelRouteNotStartedProofIdentity(
                    schema_version="model-route-not-started-proof-v1",
                    chain_id=identity.chain_id,
                    candidate_ordinal=lifecycle.candidate_ordinal,
                    global_attempt=expected,
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
                if proof_identity.digest() != proof.proof_digest:
                    raise ValueError("proof identity mismatch")
        except (ValueError, TypeError):
            raise settlement_replay_error(state_name) from None
        attempts.append(attempt)
    return final_route, attempts
