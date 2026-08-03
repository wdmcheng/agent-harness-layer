"""耐久 settlement payload 的统一发布前校验与安全重放。"""

from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError

from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
    ModelRouteChainExhaustedDetail,
    ValidatedSettlementResult,
)
from agent_harness.models._settlement_evidence_validation import (
    validate_settlement_evidence_nesting,
)
from agent_harness.models._settlement_publication import SettlementPublicationMixin
from agent_harness.models.providers import ModelResponse
from agent_harness.models.structured import (
    StructuredOutputReplayIdentity,
    canonical_structured_json,
)
from agent_harness.models.usage import (
    ModelUsageEvidence,
    UsageInvocationReplayError,
)
from agent_harness.storage.evidence_repositories import UsageSettlementClaim


class SettlementValidationMixin(SettlementPublicationMixin):
    """先完整验证 evidence/outcome/failure/response，再允许任何 final 副作用。"""

    async def _resume_existing_settlement(
        self,
        *,
        claim: UsageSettlementClaim,
        usage_call_id: str,
    ) -> ModelResponse:
        """已有可信结果补投 event 后返回原响应；绝不重放 provider。

        只有已经耐久化的响应 DTO 可以恢复为 ``ModelResponse``。若历史记录只有 usage
        evidence 而无业务响应，必须失败而非猜测输出或重新触发外部模型调用。
        """

        if claim.state in {"result_persisted", "published"} and claim.result_json is not None:
            validated = self._validated_settlement_result(
                claim.result_json,
                state=claim.state,
                error_code=claim.error_code,
            )
        else:
            raise UsageInvocationReplayError(claim.state)
        if claim.state == "result_persisted":
            await self._publish_final(
                evidence=validated.evidence,
                usage_call_id=usage_call_id,
                outcome=validated.outcome,
                error_code=claim.error_code,
            )
        return self._replayed_response(validated, state=claim.state)

    @staticmethod
    def _chain_final_provider_identity_valid(
        *,
        started: ModelUsageEvidence,
        evidence: ModelUsageEvidence,
    ) -> bool:
        """只允许同一 frozen chain state 选择的候选改变 final provider/model。"""

        from agent_harness.models.route_chain_identity import ModelRouteChainIdentity
        from agent_harness.storage.model_route_chain_state import ModelRouteChainState

        raw_started_chain = started.decision.get("route_chain")
        raw_final_chain = evidence.decision.get("route_chain")
        if not isinstance(raw_started_chain, dict) or not isinstance(raw_final_chain, dict):
            return False
        started_chain = cast(dict[str, object], raw_started_chain)
        final_chain = cast(dict[str, object], raw_final_chain)
        if started_chain.get("identity") != final_chain.get("identity"):
            return False
        try:
            identity = ModelRouteChainIdentity.model_validate(final_chain.get("identity"))
            state = ModelRouteChainState.model_validate(final_chain.get("state"))
        except (ValueError, TypeError):
            return False
        if state.chain_id != identity.chain_id:
            return False
        candidate = identity.candidates[state.evidence_route_ordinal - 1]
        return candidate.provider == evidence.provider and candidate.model == evidence.model

    @staticmethod
    def _validated_settlement_result(
        result: dict[str, Any],
        *,
        state: str,
        error_code: str | None,
    ) -> ValidatedSettlementResult:
        """在任何发布副作用前封闭解析耐久结果，统一收敛历史脏数据错误。"""

        raw_evidence = result.get("evidence")
        raw_started = result.get("started")
        if not isinstance(raw_evidence, dict) or not isinstance(raw_started, dict):
            raise UsageInvocationReplayError(state)
        try:
            evidence = ModelUsageEvidence.model_validate(raw_evidence)
            started = ModelUsageEvidence.model_validate(raw_started)
        except ValidationError:
            raise UsageInvocationReplayError(state) from None
        if (
            evidence.usage_kind != started.usage_kind
            or evidence.tenant_id != started.tenant_id
            or evidence.run_id != started.run_id
            or evidence.agent_id != started.agent_id
            or evidence.request_id != started.request_id
            or evidence.trace_id != started.trace_id
            or (
                (evidence.provider != started.provider or evidence.model != started.model)
                and not SettlementValidationMixin._chain_final_provider_identity_valid(
                    started=started,
                    evidence=evidence,
                )
            )
        ):
            raise UsageInvocationReplayError(state)

        outcome = result.get("outcome")
        if not isinstance(outcome, str) or outcome not in {
            "completed",
            "rejected",
            "failed",
            "cancelled",
        }:
            raise UsageInvocationReplayError(state)

        # route/attempt/charge 是所有真实 outcome 的共同事实边界；必须在解析
        # response/failure 或发布 final event 前统一验证，不能只保护失败分支。
        evidence_attempt_count = validate_settlement_evidence_nesting(
            evidence,
            started=started,
            state=state,
        )
        raw_structured_summary = evidence.decision.get("structured_output")
        if raw_structured_summary is not None:
            if not isinstance(raw_structured_summary, dict):
                raise UsageInvocationReplayError(state)
            try:
                structured_replay = StructuredOutputReplayIdentity.model_validate(
                    result.get("structured_replay")
                )
            except Exception:
                raise UsageInvocationReplayError(state) from None
            summary = cast(dict[str, object], raw_structured_summary)
            if (
                summary.get("replay_identity") != structured_replay.digest
                or summary.get("schema_identity") != structured_replay.schema_identity.to_payload()
                or summary.get("status") != structured_replay.final_status
                or summary.get("repair_limit") != structured_replay.repair_limit
                or summary.get("repair_count") != structured_replay.repair_count
                or summary.get("provider_request_count") != structured_replay.provider_request_count
            ):
                raise UsageInvocationReplayError(state)
            raw_attempts = evidence.decision.get("attempts")
            if not isinstance(raw_attempts, list):
                raise UsageInvocationReplayError(state)
            for raw_attempt_value in cast(list[object], raw_attempts):
                if not isinstance(raw_attempt_value, dict):
                    raise UsageInvocationReplayError(state)
                raw_attempt = cast(dict[str, object], raw_attempt_value)
                detail_value = raw_attempt.get("structured_output")
                if not isinstance(detail_value, dict):
                    raise UsageInvocationReplayError(state)
                detail = cast(dict[str, object], detail_value)
                if detail.get("repair_ordinal") == 0 and (
                    detail.get("prompt_digest") != structured_replay.prompt_digest
                ):
                    raise UsageInvocationReplayError(state)
                proof_value = detail.get("not_started_proof")
                proof = (
                    cast(dict[str, object], proof_value) if isinstance(proof_value, dict) else None
                )
                if proof_value is not None and (
                    proof is None
                    or proof.get("usage_call_id") != structured_replay.usage_call_id
                    or proof.get("operation_identity_digest")
                    != structured_replay.operation_identity_digest
                    or proof.get("route_digest") != structured_replay.route_digest
                    or proof.get("schema_identity")
                    != structured_replay.schema_identity.to_payload()
                ):
                    raise UsageInvocationReplayError(state)
        elif "structured_replay" in result:
            raise UsageInvocationReplayError(state)

        if error_code in ModelProviderInvocationError.stable_codes:
            # 稳定错误身份先于任何 payload 解释；失败记录绝不能用伪造 response
            # 覆盖原错误，否则重放会把已失败的外部副作用误报为成功。
            expected_failure_outcome = (
                "cancelled" if error_code == "model.invocation_cancelled" else "failed"
            )
            if outcome != expected_failure_outcome or "response" in result:
                raise UsageInvocationReplayError(state)
            failure_value = result.get("failure")
            if not isinstance(failure_value, dict):
                raise UsageInvocationReplayError(state)
            failure = cast(dict[str, object], failure_value)
            expected_failure_fields = {
                "error_code",
                "provider_called",
                "attempt_count",
                "latency_ms",
            }
            if error_code == "model.route_chain_exhausted":
                expected_failure_fields.add("detail")
            if set(failure) != expected_failure_fields:
                raise UsageInvocationReplayError(state)
            provider_called = failure.get("provider_called")
            attempt_count = failure.get("attempt_count")
            latency_ms = failure.get("latency_ms")
            try:
                exhausted_detail = (
                    ModelRouteChainExhaustedDetail.model_validate(failure.get("detail"))
                    if error_code == "model.route_chain_exhausted"
                    else None
                )
            except Exception as exc:
                raise UsageInvocationReplayError(state) from exc
            if exhausted_detail is not None:
                raw_chain = evidence.decision.get("route_chain")
                raw_identity = (
                    cast(dict[str, object], raw_chain).get("identity")
                    if isinstance(raw_chain, dict)
                    else None
                )
                if (
                    failure.get("detail") != evidence.decision.get("route_chain_exhausted")
                    or not isinstance(raw_identity, dict)
                    or cast(dict[str, object], raw_identity).get("chain_id")
                    != exhausted_detail.chain_id
                ):
                    raise UsageInvocationReplayError(state)
            evidence_provider_called = evidence.decision.get("provider_called")
            raw_structured = evidence.decision.get("structured_output")
            structured = (
                cast(dict[str, object], raw_structured)
                if isinstance(raw_structured, dict)
                else None
            )
            if (
                failure.get("error_code") != error_code
                or not isinstance(provider_called, bool)
                or isinstance(attempt_count, bool)
                or not isinstance(attempt_count, int)
                or attempt_count < 0
                or (
                    structured is None
                    and error_code != "model.route_chain_exhausted"
                    and provider_called != (attempt_count > 0)
                )
                or (
                    structured is not None
                    and not (
                        isinstance(structured.get("provider_request_count"), int)
                        and provider_called == (cast(int, structured["provider_request_count"]) > 0)
                        or structured.get("status") == "needs_review"
                        and structured.get("provider_request_count") is None
                        and provider_called is True
                    )
                )
                or (
                    latency_ms is not None
                    and (
                        isinstance(latency_ms, bool)
                        or not isinstance(latency_ms, int)
                        or latency_ms < 0
                    )
                )
                # failure 是便于错误重放的封闭摘要，不能成为第二份事实源；
                # 它必须逐值匹配已经解析的公开 evidence 后才能越过发布边界。
                or not isinstance(evidence_provider_called, bool)
                or provider_called != evidence_provider_called
                or attempt_count != evidence_attempt_count
                or latency_ms != evidence.latency_ms
            ):
                raise UsageInvocationReplayError(state)
            return ValidatedSettlementResult(
                evidence=evidence,
                outcome=outcome,
                response=None,
                failure=ModelProviderInvocationError(
                    error_code,
                    provider_called=provider_called,
                    attempt_count=attempt_count,
                    latency_ms=latency_ms,
                    detail=exhausted_detail,
                ),
            )

        if "failure" in result or outcome == "failed":
            raise UsageInvocationReplayError(state)
        raw_response = result.get("response")
        response_required = outcome == "completed" or (
            outcome == "rejected" and error_code == "model.policy_required"
        )
        if outcome == "completed" and error_code is not None:
            raise UsageInvocationReplayError(state)
        if outcome == "rejected" and error_code not in {
            "budget.reservation_rejected",
            "model.policy_required",
        }:
            raise UsageInvocationReplayError(state)
        if not response_required:
            if raw_response is not None or outcome != "rejected":
                raise UsageInvocationReplayError(state)
            response = None
        else:
            if not isinstance(raw_response, dict):
                raise UsageInvocationReplayError(state)
            try:
                response = ModelResponse.model_validate(raw_response)
            except ValidationError:
                raise UsageInvocationReplayError(state) from None
            if response.provider != evidence.provider or response.model != evidence.model:
                raise UsageInvocationReplayError(state)
            if (outcome == "rejected") != (response.decision.action == "policy_required"):
                raise UsageInvocationReplayError(state)
            SettlementValidationMixin._validate_structured_response(
                response=response,
                evidence=evidence,
                state=state,
            )
        return ValidatedSettlementResult(
            evidence=evidence,
            outcome=outcome,
            response=response,
            failure=None,
        )

    @staticmethod
    def _validate_structured_response(
        *,
        response: ModelResponse,
        evidence: ModelUsageEvidence,
        state: str,
    ) -> None:
        """绑定 valid result、canonical text、summary 与 attempt ordinals。"""

        raw_summary = evidence.decision.get("structured_output")
        if raw_summary is None:
            if response.structured_output is not None:
                raise UsageInvocationReplayError(state)
            return
        if not isinstance(raw_summary, dict) or response.structured_output is None:
            raise UsageInvocationReplayError(state)
        summary = cast(dict[str, object], raw_summary)
        result = response.structured_output
        raw_attempts = evidence.decision.get("attempts")
        response_attempts = [item.model_dump(mode="json") for item in response.attempts]
        if (
            summary.get("status") != "valid"
            or summary.get("schema_identity") != result.schema_identity.to_payload()
            or summary.get("repair_count") != result.repair_count
            or summary.get("provider_request_count") != result.provider_request_count
            or summary.get("replay_identity") != result.replay_identity
            or summary.get("error_code") is not None
            or response.output_text != canonical_structured_json(result.value)
            or not isinstance(raw_attempts, list)
            or response_attempts != raw_attempts
        ):
            raise UsageInvocationReplayError(state)

    @staticmethod
    def _replayed_response(
        result: ValidatedSettlementResult,
        *,
        state: str,
    ) -> ModelResponse:
        """从已校验的耐久结果恢复响应或稳定失败；无法证明时严格拒绝重放。"""

        if result.failure is not None:
            raise result.failure
        if result.response is not None:
            return result.response
        # pre-0016 result 没有可恢复业务 response 时必须 fail closed，不能把
        # usage evidence 猜成 provider 输出或再次调用 provider。
        raise UsageInvocationReplayError(state)

    @staticmethod
    def validate_durable_settlement(
        result: dict[str, Any],
        *,
        state: str,
        error_code: str | None,
    ) -> ModelUsageEvidence:
        """让恢复路径之外的 producer 复用同一完整校验边界。"""

        return SettlementValidationMixin._validated_settlement_result(
            result,
            state=state,
            error_code=error_code,
        ).evidence


def validate_durable_model_settlement(
    result: dict[str, Any],
    *,
    state: str,
    error_code: str | None,
) -> ModelUsageEvidence:
    """完整验证耐久模型结算，并只返回已绑定 started/final 的 usage evidence。

    恢复路径和独立验收 producer 必须共用同一校验边界，避免后者只解析 DTO
    外形却漏掉 route-chain identity、attempt proof 或 budget charge 的交叉约束。
    """

    return SettlementValidationMixin.validate_durable_settlement(
        result,
        state=state,
        error_code=error_code,
    )
