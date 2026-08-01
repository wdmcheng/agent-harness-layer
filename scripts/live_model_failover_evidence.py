"""从 durable route-chain 与 usage outbox 组装去敏 live failover 证据。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from agent_harness.models import UsageInvocationReplayError, validate_durable_model_settlement
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.model_route_chain_state import (
    ModelRouteCandidateState,
    ModelRouteChainState,
)


def candidate_payload(
    candidate: ModelRouteCandidateState,
    *,
    attempt_count: int,
) -> dict[str, object]:
    """从 durable state 生成去敏 candidate 证据，不读取 endpoint 或 credential。"""

    attempts = tuple(candidate.not_started_proofs)
    state = str(candidate.state)
    outcome = {
        "not_started": "not_started",
        "completed": "completed",
        "unknown": "unknown",
    }.get(state, "not_called")
    if outcome == "not_called" and attempt_count:
        # 异常可能发生在 started identity 已提交、candidate 关闭前；artifact 必须
        # 保留这个高水位为 unknown，不能降成零调用或丢掉 frozen identity。
        outcome = "unknown"
    return {
        "ordinal": int(candidate.ordinal),
        "deployment_id": str(candidate.deployment_id),
        "provider": str(candidate.provider),
        "model": str(candidate.model),
        "outcome": outcome,
        "attempt_count": attempt_count,
        "not_started_proof_count": len(attempts),
        "request_sent": bool(candidate.request_sent),
        "response_observed": bool(candidate.http_response_observed),
        "not_started_reason": (str(candidate.reason) if outcome == "not_started" else None),
        "http_status": candidate.http_status,
    }


async def load_durable_failover_evidence(
    *,
    storage: SQLAlchemyStorage,
    tenant_id: str,
    run_id: str,
    usage_call_id: str,
) -> tuple[ModelRouteChainState, dict[str, object]]:
    """读取同一调用的 chain 与 usage outbox，并拒绝两份耐久事实漂移。"""

    async with storage.uow() as uow:
        state = await uow.shared_budget.get_model_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
        )
        usage_record = await uow.evidence_outbox.get_usage(
            tenant_id=tenant_id,
            usage_call_id=usage_call_id,
        )
        result = dict(usage_record.result_json) if usage_record.result_json is not None else None
        usage_state = str(usage_record.state)
        usage_error_code = usage_record.error_code
    if state is None:
        raise ValueError("live failover did not persist route-chain state")
    try:
        if result is None:
            raise TypeError("usage settlement result must be a mapping")
        evidence = validate_durable_model_settlement(
            result,
            state=usage_state,
            error_code=usage_error_code,
        )
        route_chain = evidence.decision["route_chain"]
        if not isinstance(route_chain, Mapping):
            raise TypeError("route-chain evidence must be a mapping")
        route_chain_payload = cast(Mapping[str, object], route_chain)
        evidence_state = ModelRouteChainState.model_validate(route_chain_payload.get("state"))
    except (KeyError, TypeError, ValueError, UsageInvocationReplayError):
        raise ValueError("live failover usage evidence is missing or invalid") from None
    if evidence_state != state:
        raise ValueError("live failover route-chain and usage evidence do not match")

    candidates = [
        candidate_payload(
            item,
            attempt_count=sum(
                attempt.candidate_ordinal == item.ordinal
                for attempt in evidence_state.attempt_lifecycle
            ),
        )
        for item in evidence_state.candidates
    ]
    return evidence_state, {
        "chain_id": evidence_state.chain_id,
        "selected_ordinal": evidence_state.selected_ordinal,
        "candidates": candidates,
        "attempts": [
            {
                "attempt": item.attempt,
                "candidate_ordinal": item.candidate_ordinal,
                "not_started_proof_count": int(item.lifecycle_state == "not_started_proven"),
            }
            for item in evidence_state.attempt_lifecycle
        ],
        "usage": {
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
            "cost_usd": evidence.cost_usd,
            "cost_status": evidence.cost_status,
        },
    }


__all__ = ["candidate_payload", "load_durable_failover_evidence"]
