"""Structured调用的provider-neutral结果、evidence与最终结算。"""

from __future__ import annotations

import hashlib
from typing import Any, Literal, cast

from agent_harness.models._invocation_structured_support import (
    ModelInvocationStructuredSupportMixin,
)
from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
    SettlementStart,
)
from agent_harness.models._structured_settlement_evidence_models import (
    StructuredSettlementSummary,
    StructuredSettlementValidationIssue,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelResponse,
    StructuredModelAttemptEvidence,
)
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    StructuredOutputReplayIdentity,
    StructuredOutputResult,
    canonical_structured_json,
    structured_digest,
)
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext

StructuredTerminalStatus = Literal[
    "valid",
    "invalid",
    "extra_fields",
    "repair_exhausted",
    "failed",
    "needs_review",
]


class ModelInvocationStructuredResultMixin(ModelInvocationStructuredSupportMixin):
    """把已结束的transport控制流投影为唯一耐久终态。"""

    async def _finalize_structured_execution(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        operation_identity_digest: str,
        schema: OutputSchemaDefinition,
        plan: ModelRoutePlan,
        initial_prompt: str,
        effective_limit: int,
        transport_limit: int,
        provider_request_limit: int,
        route_evidence: dict[str, object],
        structured_route_identity: dict[str, Any],
        route_digest: str,
        settlement: SettlementStart,
        attempts: list[StructuredModelAttemptEvidence],
        validation_issues: list[dict[str, str]],
        provider_request_count: int,
        final_status: StructuredTerminalStatus,
        error_code: str | None,
        valid_value: dict[str, Any] | None,
    ) -> ModelResponse:
        """构造replay/evidence并以completed或稳定失败完成同一settlement。"""

        repair_count = max(
            (item.structured_output.repair_ordinal for item in attempts),
            default=0,
        )
        replay = StructuredOutputReplayIdentity(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            prompt_digest=hashlib.sha256(initial_prompt.encode("utf-8")).hexdigest(),
            deployment_id=plan.deployment_id,
            provider=plan.provider,
            model=plan.model,
            route_digest=route_digest,
            schema_identity=schema.identity,
            transport_attempt_limit=transport_limit,
            repair_limit=effective_limit,
            repair_count=repair_count,
            provider_request_count=provider_request_count,
            final_status=final_status,
            value_digest=structured_digest(valid_value) if valid_value is not None else None,
        )
        summary = self._structured_attempt_summary(
            attempts=attempts,
            plan=plan,
            provider_called=provider_request_count > 0,
        )
        structured_summary = StructuredSettlementSummary(
            schema_version="structured-output-evidence-v1",
            schema_identity=schema.identity,
            status=final_status,
            repair_limit=effective_limit,
            repair_count=repair_count,
            provider_request_limit=provider_request_limit,
            provider_request_count=provider_request_count,
            replay_identity=replay.digest,
            validation_issues=[
                StructuredSettlementValidationIssue.model_validate(item)
                for item in validation_issues
            ],
            error_code=error_code,
        ).model_dump(mode="json")
        evidence = ModelUsageEvidence(
            usage_kind="model",
            tenant_id=context.tenant_id,
            provider=plan.provider,
            model=plan.model,
            input_tokens=cast(int | None, summary["input_tokens"]),
            output_tokens=cast(int | None, summary["output_tokens"]),
            cost_usd=cast(float | None, summary["cost_usd"]),
            cost_status=cast(Any, summary["cost_status"]),
            latency_ms=sum(item.latency_ms for item in attempts),
            decision=self._safe_decision(
                plan.decision.to_payload(),
                {"route": route_evidence},
                {"structured_route_identity": structured_route_identity},
                {"provider_called": provider_request_count > 0},
                {
                    "attempts": cast(Any, summary["attempts"]),
                    "budget_charge": cast(Any, summary["budget_charge"]),
                    "structured_output": structured_summary,
                },
            ),
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )
        if final_status == "valid":
            assert valid_value is not None
            response = ModelResponse(
                provider=plan.provider,
                model=plan.model,
                output_text=canonical_structured_json(valid_value),
                decision=plan.decision,
                token_usage={
                    "input_tokens": evidence.input_tokens or 0,
                    "output_tokens": evidence.output_tokens or 0,
                },
                latency_ms=evidence.latency_ms,
                cost_usd=evidence.cost_usd,
                cost_status=evidence.cost_status,
                attempts=cast(
                    list[StructuredModelAttemptEvidence | ModelAttemptEvidence], attempts
                ),
                structured_output=StructuredOutputResult(
                    schema_identity=schema.identity,
                    value=valid_value,
                    repair_count=repair_count,
                    provider_request_count=provider_request_count,
                    replay_identity=replay.digest,
                ),
            )
            await self._finalize(
                evidence=evidence,
                usage_call_id=usage_call_id,
                outcome="completed",
                error_code=None,
                ownership=settlement.ownership,
                response=response,
                structured_replay=replay,
            )
            return response

        assert error_code is not None
        await self._finalize(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome="cancelled" if error_code == "model.invocation_cancelled" else "failed",
            error_code=error_code,
            ownership=settlement.ownership,
            response=None,
            structured_replay=replay,
        )
        raise ModelProviderInvocationError(
            error_code,
            provider_called=provider_request_count > 0,
            attempt_count=len(attempts),
            latency_ms=evidence.latency_ms,
        )


__all__ = ["ModelInvocationStructuredResultMixin", "StructuredTerminalStatus"]
