"""耐久 settlement evidence 的冻结 route、attempt 与 charge 校验。"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from agent_harness.models._router_contracts import ModelRoutePlan
from agent_harness.models._router_identity import route_plan_identity_payload
from agent_harness.models._settlement_chain_evidence_validation import (
    settlement_replay_error,
    validate_chain_attempts,
)
from agent_harness.models._settlement_evidence_models import (
    ATTEMPT_FIELDS,
    BUDGET_CHARGE_FIELDS,
    STRUCTURED_ATTEMPT_FIELDS,
    SettlementBudgetChargeEvidence,
    SettlementChainAttemptEvidence,
    SettlementRouteEvidence,
)
from agent_harness.models._structured_settlement_evidence_models import (
    StructuredSettlementAttemptEvidence,
    StructuredSettlementRouteEvidence,
    StructuredSettlementSummary,
)
from agent_harness.models.providers import ModelAttemptEvidence
from agent_harness.models.structured import structured_digest
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
    structured_mode = "structured_output" in decision or "structured_output" in started.decision
    provider_called = decision.get("provider_called")
    raw_structured_summary = decision.get("structured_output")
    structured_summary_mapping = (
        cast(dict[str, object], raw_structured_summary)
        if isinstance(raw_structured_summary, dict)
        else None
    )
    structured_started_crash = (
        structured_mode
        and structured_summary_mapping is not None
        and structured_summary_mapping.get("status") == "needs_review"
        and structured_summary_mapping.get("provider_request_count") is None
        and raw_attempts == []
    )
    controlled_real = evidence.provider == "openai-compatible" and (
        provider_called is True or isinstance(started_route, dict)
    )
    if raw_attempts is None and raw_budget_charge is None:
        if controlled_real or structured_mode:
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
            route = (
                StructuredSettlementRouteEvidence.model_validate(
                    cast(dict[object, object], raw_route)
                )
                if structured_mode
                else SettlementRouteEvidence.model_validate(cast(dict[object, object], raw_route))
            )
            if route.provider != evidence.provider or route.model != evidence.model:
                raise settlement_replay_error(state)
            for expected, raw_attempt in enumerate(cast(list[object], raw_attempts), start=1):
                if not isinstance(raw_attempt, dict):
                    raise settlement_replay_error(state)
                attempt_payload = cast(dict[object, object], raw_attempt)
                expected_fields = STRUCTURED_ATTEMPT_FIELDS if structured_mode else ATTEMPT_FIELDS
                if set(attempt_payload) != expected_fields:
                    raise settlement_replay_error(state)
                attempt = (
                    StructuredSettlementAttemptEvidence.model_validate(attempt_payload)
                    if structured_mode
                    else ModelAttemptEvidence.model_validate(attempt_payload)
                )
                if attempt.attempt != expected:
                    raise settlement_replay_error(state)
                attempts.append(attempt)
        budget_payload = cast(dict[object, object], raw_budget_charge)
        if set(budget_payload) != BUDGET_CHARGE_FIELDS:
            raise settlement_replay_error(state)
        budget_charge = SettlementBudgetChargeEvidence.model_validate(budget_payload)
    except (ValueError, TypeError):
        raise settlement_replay_error(state) from None

    if (controlled_real or structured_mode) and (
        not isinstance(provider_called, bool)
        or provider_called
        and not attempts
        and not structured_started_crash
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
            or isinstance(attempt, StructuredSettlementAttemptEvidence)
            and attempt.structured_output.not_started_proof is not None
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

    if structured_started_crash:
        # started/send 边界崩溃没有可持久化 attempt 下界，但可能的第一个请求仍
        # 必须把 reservation 保持 unknown；ordinal 1 只标识账本未决槽位。
        unresolved.add(1)
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
    if structured_mode:
        _validate_structured_evidence(
            evidence=evidence,
            started=started,
            attempts=attempts,
            route=cast(StructuredSettlementRouteEvidence, route),
            state=state,
        )
    return len(attempts)


def _validate_structured_evidence(
    *,
    evidence: ModelUsageEvidence,
    started: ModelUsageEvidence,
    attempts: list[ModelAttemptEvidence],
    route: StructuredSettlementRouteEvidence,
    state: str,
) -> None:
    """交叉校验 structured summary、attempt ordinals 与 provider request 计数。"""

    raw_summary = evidence.decision.get("structured_output")
    raw_started_summary = started.decision.get("structured_output")
    raw_route_identity = evidence.decision.get("structured_route_identity")
    raw_started_route_identity = started.decision.get("structured_route_identity")
    if (
        not isinstance(raw_summary, dict)
        or not isinstance(raw_started_summary, dict)
        or not isinstance(raw_route_identity, dict)
        or not isinstance(raw_started_route_identity, dict)
        or raw_route_identity != raw_started_route_identity
    ):
        raise settlement_replay_error(state)
    try:
        summary = StructuredSettlementSummary.model_validate(raw_summary)
        started_summary = StructuredSettlementSummary.model_validate(raw_started_summary)
        identity_plan = ModelRoutePlan.model_validate(raw_started_route_identity)
    except (ValueError, TypeError):
        raise settlement_replay_error(state) from None
    if (
        started_summary.status != "started"
        or started_summary.schema_identity != summary.schema_identity
        or started_summary.repair_limit != route.repair_limit
        or started_summary.provider_request_limit != route.provider_request_limit
        or summary.repair_limit != route.repair_limit
        or summary.provider_request_limit != route.provider_request_limit
        or route_plan_identity_payload(identity_plan) != raw_started_route_identity
        or identity_plan.capability != "structured_output"
        or identity_plan.deployment_id != route.deployment_id
        or identity_plan.provider_kind != route.provider_kind
        or identity_plan.provider != route.provider
        or identity_plan.model != route.model
        or identity_plan.repair_limit != route.repair_limit
        or identity_plan.provider_request_limit != route.provider_request_limit
        or identity_plan.max_attempts != route.max_attempts
    ):
        raise settlement_replay_error(state)
    typed_attempts = [cast(StructuredSettlementAttemptEvidence, item) for item in attempts]
    if any(not isinstance(item, StructuredSettlementAttemptEvidence) for item in attempts):
        raise settlement_replay_error(state)
    if typed_attempts and typed_attempts[0].structured_output.repair_ordinal != 0:
        raise settlement_replay_error(state)
    repair_prompt_digests: dict[int, str] = {}
    repair_trigger_codes: dict[int, tuple[str, ...]] = {}
    previous: StructuredSettlementAttemptEvidence | None = None
    route_digest = structured_digest(cast(dict[str, object], raw_started_route_identity))
    for expected, attempt in enumerate(typed_attempts, start=1):
        detail = attempt.structured_output
        if (
            attempt.attempt != expected
            or detail.schema_identity != summary.schema_identity
            or detail.repair_ordinal > route.repair_limit
        ):
            raise settlement_replay_error(state)
        earlier = typed_attempts[: expected - 1]
        expected_repair = max(
            (item.structured_output.repair_ordinal for item in earlier),
            default=0,
        )
        if detail.repair_ordinal not in {expected_repair, expected_repair + 1}:
            raise settlement_replay_error(state)
        same_repair = [
            item
            for item in earlier
            if item.structured_output.repair_ordinal == detail.repair_ordinal
        ]
        if detail.transport_ordinal != len(same_repair) + 1:
            raise settlement_replay_error(state)
        if detail.transport_ordinal > route.max_attempts:
            raise settlement_replay_error(state)
        frozen_prompt = repair_prompt_digests.setdefault(
            detail.repair_ordinal, detail.prompt_digest
        )
        frozen_triggers = repair_trigger_codes.setdefault(
            detail.repair_ordinal, detail.repair_trigger_codes
        )
        if detail.prompt_digest != frozen_prompt or detail.repair_trigger_codes != frozen_triggers:
            raise settlement_replay_error(state)
        if any(
            digest == detail.prompt_digest and ordinal != detail.repair_ordinal
            for ordinal, digest in repair_prompt_digests.items()
        ):
            raise settlement_replay_error(state)
        proof = detail.not_started_proof
        if proof is not None and (
            proof.usage_call_id == ""
            or proof.route_digest != route_digest
            or proof.schema_identity != summary.schema_identity
            or proof.prompt_digest != detail.prompt_digest
            or proof.attempt != attempt.attempt
            or proof.repair_ordinal != detail.repair_ordinal
            or proof.transport_ordinal != detail.transport_ordinal
        ):
            raise settlement_replay_error(state)
        if previous is not None:
            previous_detail = previous.structured_output
            if detail.repair_ordinal == previous_detail.repair_ordinal:
                # Transport retry 只允许发生在带 proof 的 prepare failure 之后；
                # send 一旦发生，同一 repair prompt 不得再次发送。
                previous_proof = previous_detail.not_started_proof
                if previous_proof is None or previous_proof.kind != "client_prepare_not_started":
                    raise settlement_replay_error(state)
            elif (
                detail.repair_ordinal != previous_detail.repair_ordinal + 1
                or previous_detail.not_started_proof is not None
                or previous.outcome != "completed"
                or previous.side_effect_state != "started"
                or previous.completion_observed is not True
                or previous_detail.cleanup_status != "completed"
                or not previous_detail.validation_codes
                or detail.repair_trigger_codes != previous_detail.validation_codes
            ):
                raise settlement_replay_error(state)
        previous = attempt
    provider_requests = sum(
        item.structured_output.not_started_proof is None for item in typed_attempts
    )
    repair_count = max(
        (item.structured_output.repair_ordinal for item in typed_attempts),
        default=0,
    )
    if (
        summary.provider_request_count is not None
        and summary.provider_request_count != provider_requests
        or summary.repair_count is not None
        and summary.repair_count != repair_count
        or provider_requests > route.provider_request_limit
        or summary.status != "needs_review"
        and evidence.decision.get("provider_called") != (provider_requests > 0)
        or started.decision.get("provider_called") is not False
    ):
        raise settlement_replay_error(state)
    last = typed_attempts[-1] if typed_attempts else None
    last_detail = last.structured_output if last is not None else None
    if summary.status in {"valid", "invalid", "extra_fields", "repair_exhausted"} and (
        last is None
        or last_detail is None
        or last_detail.not_started_proof is not None
        or last.side_effect_state != "started"
        or last.outcome != "completed"
        or last.completion_observed is not True
        or last_detail.cleanup_status != "completed"
    ):
        raise settlement_replay_error(state)
    final_issue_codes = tuple(sorted({item.code for item in summary.validation_issues}))
    if summary.status == "valid" and (last_detail is None or last_detail.validation_codes != ()):
        raise settlement_replay_error(state)
    if summary.status in {"invalid", "extra_fields", "repair_exhausted"} and (
        last_detail is None
        or not last_detail.validation_codes
        or last_detail.validation_codes != final_issue_codes
    ):
        raise settlement_replay_error(state)
    has_unresolved_send = any(
        item.structured_output.not_started_proof is None
        and (
            item.side_effect_state != "started"
            or item.outcome not in {"completed", "failed"}
            or item.completion_observed is not True
            or item.structured_output.cleanup_status != "completed"
            or item.input_tokens is None
            or item.output_tokens is None
            or route.input_token_price_usd is not None
            and item.cost_usd is None
        )
        for item in typed_attempts
    )
    if has_unresolved_send and summary.status != "needs_review":
        raise settlement_replay_error(state)
    if (
        summary.status == "failed"
        and summary.error_code == "model.provider_failed"
        and summary.provider_request_count not in {None, 0}
        and (
            last is None
            or last.side_effect_state != "started"
            or last.outcome != "failed"
            or last.completion_observed is not True
            or last.error_code != summary.error_code
        )
    ):
        raise settlement_replay_error(state)
    if summary.status == "needs_review" and (
        typed_attempts and last_detail is not None and last_detail.not_started_proof is not None
    ):
        proof = last_detail.not_started_proof
        durable_mark_unknown = (
            summary.provider_request_count == 0
            and summary.error_code == "model.provider_side_effect_unknown"
            and proof.kind == "cancelled_before_send"
            and last is not None
            and last.side_effect_state == "not_started"
            and last.outcome == "unknown"
        )
        if not durable_mark_unknown:
            raise settlement_replay_error(state)
    if summary.status == "failed" and summary.provider_request_count == 0:
        if summary.error_code == "model.provider_retry_exhausted":
            current_repair = [
                item
                for item in typed_attempts
                if item.structured_output.repair_ordinal == repair_count
            ]
            if len(current_repair) != route.max_attempts or any(
                item.structured_output.not_started_proof is None
                or item.structured_output.not_started_proof.kind != "client_prepare_not_started"
                for item in current_repair
            ):
                raise settlement_replay_error(state)
        elif summary.error_code == "model.invocation_cancelled":
            if (
                last_detail is None
                or last_detail.not_started_proof is None
                or last_detail.not_started_proof.kind != "cancelled_before_send"
            ):
                raise settlement_replay_error(state)
