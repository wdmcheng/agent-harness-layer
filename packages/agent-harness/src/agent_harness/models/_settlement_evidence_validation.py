"""耐久 settlement evidence 的冻结 route、attempt 与 charge 校验。"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from agent_harness.models._settlement_chain_evidence_validation import (
    settlement_replay_error,
    validate_chain_attempts,
)
from agent_harness.models._settlement_evidence_models import (
    ATTEMPT_FIELDS,
    BUDGET_CHARGE_FIELDS,
    SettlementBudgetChargeEvidence,
    SettlementChainAttemptEvidence,
    SettlementRouteEvidence,
)
from agent_harness.models.providers import ModelAttemptEvidence
from agent_harness.models.usage import ModelUsageEvidence


def validate_settlement_evidence_nesting(
    evidence: ModelUsageEvidence,
    *,
    started: ModelUsageEvidence,
    state: str,
) -> int:
    """验证 5.29 嵌套证据并返回可信 attempt 数量。

    fake、零 provider 拒绝与历史 evidence 可以同时省略嵌套字段；只要任一字段出现，
    就必须连同 typed route 按封闭形状、冻结公式和逐 attempt charge 规则完整对账。
    """

    decision = evidence.decision
    raw_attempts = decision.get("attempts")
    raw_budget_charge = decision.get("budget_charge")
    started_route = started.decision.get("route")
    chain_mode = "route_chain" in decision or "route_chain" in started.decision
    provider_called = decision.get("provider_called")
    controlled_real = evidence.provider == "openai-compatible" and (
        provider_called is True or isinstance(started_route, dict)
    )
    if raw_attempts is None and raw_budget_charge is None:
        if controlled_real:
            raise settlement_replay_error(state)
        return int(decision.get("provider_called") is True)
    if not isinstance(raw_attempts, list) or not isinstance(raw_budget_charge, dict):
        raise settlement_replay_error(state)

    attempts: list[ModelAttemptEvidence] = []
    try:
        if chain_mode:
            route, chain_attempts = validate_chain_attempts(
                evidence=evidence,
                started=started,
                raw_attempts=cast(list[object], raw_attempts),
                state_name=state,
            )
            attempts.extend(chain_attempts)
        else:
            raw_route = decision.get("route")
            if not isinstance(raw_route, dict):
                raise settlement_replay_error(state)
            if not isinstance(started_route, dict) or raw_route != started_route:
                # legacy final route 没有 chain proofs，必须逐值继承 started anchor。
                raise settlement_replay_error(state)
            route = SettlementRouteEvidence.model_validate(cast(dict[object, object], raw_route))
            if route.provider != evidence.provider or route.model != evidence.model:
                raise settlement_replay_error(state)
            for expected, raw_attempt in enumerate(cast(list[object], raw_attempts), start=1):
                if not isinstance(raw_attempt, dict):
                    raise settlement_replay_error(state)
                attempt_payload = cast(dict[object, object], raw_attempt)
                if set(attempt_payload) != ATTEMPT_FIELDS:
                    raise settlement_replay_error(state)
                attempt = ModelAttemptEvidence.model_validate(attempt_payload)
                if attempt.attempt != expected:
                    raise settlement_replay_error(state)
                attempts.append(attempt)
        budget_payload = cast(dict[object, object], raw_budget_charge)
        if set(budget_payload) != BUDGET_CHARGE_FIELDS:
            raise settlement_replay_error(state)
        budget_charge = SettlementBudgetChargeEvidence.model_validate(budget_payload)
    except (ValueError, TypeError):
        raise settlement_replay_error(state) from None

    if controlled_real and (
        not isinstance(provider_called, bool) or provider_called and not attempts
    ):
        raise settlement_replay_error(state)

    cost_enabled = route.input_token_price_usd is not None
    unresolved: set[int] = set()
    charged_tokens = 0
    charged_cost = 0.0
    aggregate_input = 0
    aggregate_output = 0
    aggregate_cost = 0.0
    aggregate_cost_status = "reported"

    for attempt in attempts:
        if attempt.error_code is not None and not attempt.error_code.strip():
            raise settlement_replay_error(state)
        if attempt.cost_usd is None:
            if attempt.cost_status != "unavailable":
                raise settlement_replay_error(state)
        elif attempt.cost_status == "unavailable":
            raise settlement_replay_error(state)

        proven_not_started = (
            isinstance(attempt, SettlementChainAttemptEvidence)
            and attempt.not_started_reason is not None
        )
        if proven_not_started:
            if attempt.outcome == "completed" or any(
                value is not None
                for value in (attempt.input_tokens, attempt.output_tokens, attempt.cost_usd)
            ):
                raise settlement_replay_error(state)
            if attempt.budget_charge_tokens != 0 or attempt.budget_charge_cost_usd not in {
                None,
                0,
            }:
                raise settlement_replay_error(state)
        elif attempt.side_effect_state == "not_started":
            if attempt.outcome == "completed" or any(
                value is not None
                for value in (attempt.input_tokens, attempt.output_tokens, attempt.cost_usd)
            ):
                raise settlement_replay_error(state)
            if attempt.budget_charge_tokens != 0 or attempt.budget_charge_cost_usd not in {
                None,
                0,
            }:
                raise settlement_replay_error(state)
        elif attempt.side_effect_state == "started":
            if attempt.outcome == "completed" and attempt.completion_observed is not True:
                raise settlement_replay_error(state)
            token_known = attempt.input_tokens is not None and attempt.output_tokens is not None
            if (attempt.input_tokens is None) != (attempt.output_tokens is None):
                raise settlement_replay_error(state)
            expected_tokens = (
                cast(int, attempt.input_tokens) + cast(int, attempt.output_tokens)
                if token_known
                else None
            )
            expected_cost = attempt.cost_usd if cost_enabled else None
            if (
                attempt.budget_charge_tokens != expected_tokens
                or attempt.budget_charge_cost_usd != expected_cost
            ):
                raise settlement_replay_error(state)
            if token_known:
                aggregate_input += cast(int, attempt.input_tokens)
                aggregate_output += cast(int, attempt.output_tokens)
                charged_tokens += cast(int, expected_tokens)
            if cost_enabled and attempt.cost_usd is not None:
                aggregate_cost += attempt.cost_usd
                charged_cost += attempt.cost_usd
                if attempt.cost_status == "estimated":
                    aggregate_cost_status = "estimated"
            if not token_known or (cost_enabled and attempt.cost_usd is None):
                unresolved.add(attempt.attempt)
        else:
            if (
                attempt.budget_charge_tokens is not None
                or attempt.budget_charge_cost_usd is not None
            ):
                raise settlement_replay_error(state)
            unresolved.add(attempt.attempt)

    if charged_tokens > route.reserved_token_bound:
        unresolved.update(attempt.attempt for attempt in attempts)
    if cost_enabled and Decimal(str(charged_cost)) > cast(Decimal, route.reserved_cost_bound):
        unresolved.update(attempt.attempt for attempt in attempts)

    expected_unresolved = sorted(unresolved)
    if expected_unresolved:
        if (
            budget_charge.charge_status != "unknown"
            or budget_charge.unresolved_attempts != expected_unresolved
        ):
            raise settlement_replay_error(state)
        if (
            any(
                value is not None
                for value in (evidence.input_tokens, evidence.output_tokens, evidence.cost_usd)
            )
            or evidence.cost_status != "unavailable"
        ):
            raise settlement_replay_error(state)
    else:
        expected_cost = charged_cost if cost_enabled else None
        if (
            budget_charge.charge_status != "actual"
            or budget_charge.unresolved_attempts
            or budget_charge.charged_tokens != charged_tokens
            or budget_charge.charged_cost_usd != expected_cost
        ):
            raise settlement_replay_error(state)
        provider_called = decision.get("provider_called") is True
        if evidence.input_tokens != (aggregate_input if provider_called else None):
            raise settlement_replay_error(state)
        if evidence.output_tokens != (aggregate_output if provider_called else None):
            raise settlement_replay_error(state)
        expected_evidence_cost = aggregate_cost if provider_called and cost_enabled else None
        expected_cost_status = (
            aggregate_cost_status if expected_evidence_cost is not None else "unavailable"
        )
        if (
            evidence.cost_usd != expected_evidence_cost
            or evidence.cost_status != expected_cost_status
        ):
            raise settlement_replay_error(state)
    return len(attempts)
