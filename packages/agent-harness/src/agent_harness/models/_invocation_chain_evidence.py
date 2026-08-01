"""可信未开始分类、耐久状态 I/O 与公开 chain evidence。"""

from __future__ import annotations

from typing import Literal, NoReturn, cast

from agent_harness.models._invocation_chain_base import (
    ChainRuntimeBase,
)
from agent_harness.models._route_chain_state import close_route_attempt
from agent_harness.models._router_contracts import ModelRouteCandidate, ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
    ModelRouteChainExhaustedDetail,
    RouteAttemptNotStartedFacts,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelResponse,
)
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetOperationOwnership,
)


class _ChainEvidenceMixin(ChainRuntimeBase):
    @staticmethod
    def _trusted_not_started_facts(
        exc: Exception,
        *,
        candidate: ModelRouteCandidate,
    ) -> RouteAttemptNotStartedFacts | None:
        """只接受两类封闭事实；异常文本或 HTTP status 单独出现都不能授权切换。"""

        reason = getattr(exc, "not_started_reason", None)
        status = getattr(exc, "http_status", getattr(exc, "status_code", None))
        side_effect_state = getattr(exc, "side_effect_state", None)
        completion_observed = getattr(exc, "completion_observed", None)
        if reason is None:
            if (
                side_effect_state == "not_started"
                and status is None
                and completion_observed is False
            ):
                reason = "client_not_started"
            elif (
                side_effect_state == "started"
                and status in candidate.route.cross_provider_failover_http_statuses
                and completion_observed is False
            ):
                reason = "trusted_business_not_started"
        request_sent = getattr(exc, "request_sent", reason != "client_not_started")
        response_observed = getattr(
            exc,
            "http_response_observed",
            reason == "trusted_business_not_started",
        )
        classifier_ref = getattr(
            exc,
            "classifier_ref",
            (
                candidate.route.completion_classifier_ref
                if reason == "trusted_business_not_started"
                else None
            ),
        )
        classifier_version = getattr(
            exc,
            "classifier_version",
            (
                candidate.route.completion_classifier_version
                if reason == "trusted_business_not_started"
                else None
            ),
        )
        common_false = not any(
            bool(getattr(exc, field, False))
            for field in (
                "response_identity_observed",
                "usage_observed",
                "text_observed",
                "delta_observed",
            )
        )
        if reason == "client_not_started":
            valid = (
                side_effect_state == "not_started"
                and request_sent is False
                and response_observed is False
                and status is None
                and completion_observed in {None, False}
                and classifier_ref is None
                and classifier_version is None
                and common_false
            )
        elif reason == "trusted_business_not_started":
            valid = (
                side_effect_state == "started"
                and request_sent is True
                and response_observed is True
                and status in candidate.route.cross_provider_failover_http_statuses
                and completion_observed is False
                and classifier_ref == candidate.route.completion_classifier_ref
                and classifier_version == candidate.route.completion_classifier_version
                and common_false
            )
        else:
            return None
        if not valid:
            return None
        # 受控 transport 用 ``False`` 表达连接失败时“未观察到完成”；进入
        # route-chain canonical proof 后，client_not_started 根本没有响应事实，
        # 因而必须归一为 nullable ``None``，不能把 adapter 内部判别值写入摘要。
        proof_completion_observed = None if reason == "client_not_started" else completion_observed
        return RouteAttemptNotStartedFacts(
            not_started_reason=reason,
            side_effect_state=cast(Literal["not_started", "started"], side_effect_state),
            request_sent=request_sent,
            http_response_observed=response_observed,
            http_status=status,
            response_identity_observed=bool(getattr(exc, "response_identity_observed", False)),
            usage_observed=bool(getattr(exc, "usage_observed", False)),
            text_observed=bool(getattr(exc, "text_observed", False)),
            delta_observed=bool(getattr(exc, "delta_observed", False)),
            completion_observed=proof_completion_observed,
            endpoint_policy_digest=candidate.endpoint_policy_digest,
            classifier_ref=cast(str | None, classifier_ref),
            classifier_version=cast(str | None, classifier_version),
        )

    async def _load_route_chain_state(
        self,
        context: UsageEvidenceContext,
        usage_call_id: str,
    ) -> ModelRouteChainState:
        """从 shared-budget 公共 seam 读取刚提交的完整 state。"""

        async with self._storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                usage_call_id=usage_call_id,
            )
        if state is None:
            raise RuntimeError("route-chain settlement state is missing")
        return state

    async def _persist_route_chain_state(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        state: ModelRouteChainState,
        method: str,
        proof_state: ModelRouteChainState | None = None,
    ) -> ModelRouteChainState:
        """每次状态推进独占一个 owner UoW，commit 返回后才越过下个 provider 边界。"""

        async with self._storage.uow() as uow:
            operation = getattr(uow.shared_budget, method)
            if proof_state is None:
                record = await operation(
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    usage_call_id=usage_call_id,
                    state=state,
                )
            else:
                record = await operation(
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    usage_call_id=usage_call_id,
                    proof_state=proof_state,
                    state=state,
                )
            await uow.commit()
        if method == "append_model_route_attempt_started" and record.replayed:
            # Exact replay means该 identity 已由另一个执行者或 commit-ack未知窗口
            # 创建；两者都不得再次越过 provider 边界。
            raise BudgetOperationConflict
        if record.route_chain_state is None:
            raise RuntimeError("route-chain mutation omitted durable state")
        return record.route_chain_state

    async def _raise_cleanup_route_attempt_unknown(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        state: ModelRouteChainState,
        candidate_ordinal: int,
        ownership: BudgetOperationOwnership | None,
        response: ModelResponse,
        delta_observed: bool,
    ) -> NoReturn:
        """成功响应后的 cleanup 不确定性必须原子封闭为待复核，不能泄漏 raw 异常。"""

        if ownership is None:
            raise RuntimeError("route-chain settlement omitted budget ownership") from None
        global_attempt = len(state.attempt_lifecycle)
        attempt = self._global_response_attempt(response, global_attempt).model_copy(
            update={
                "side_effect_state": "unknown",
                "outcome": "unknown",
                "completion_observed": True,
                "error_code": "model.provider_side_effect_unknown",
            }
        )
        state = close_route_attempt(
            state,
            candidate_ordinal=candidate_ordinal,
            lifecycle_state="unknown",
            response_observed=True,
            request_sent=True,
            usage_observed=True,
            text_observed=True,
            delta_observed=delta_observed,
            completion_observed=True,
            http_status=attempt.http_status,
            response_identity_observed=False,
        )
        review: dict[str, object] = {
            "provider_close_state": "unknown",
            "usage_finality": "complete",
            "outcome": "failed",
            "error_code": "model.provider_side_effect_unknown",
            "provider_called": True,
            "latency_ms": response.latency_ms,
            "attempts": [attempt.model_dump(mode="python")],
            "budget_charge": {
                "charged_tokens": None,
                "charged_cost_usd": None,
                "charge_status": "unknown",
                "unresolved_attempts": [global_attempt],
            },
        }
        budget_result = {"attempt_review": review}
        async with self._storage.uow() as uow:
            await uow.evidence_outbox.persist_attempt_review(
                tenant_id=context.tenant_id,
                usage_call_id=usage_call_id,
                review=review,
                error_code="model.provider_side_effect_unknown",
            )
            record = await uow.shared_budget.close_model_route_attempt(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                usage_call_id=usage_call_id,
                state=state,
            )
            if ownership.kind == "direct":
                await uow.shared_budget.settle_direct(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=ownership.budget_owner_run_id,
                    usage_call_id=usage_call_id,
                    actual_tokens=None,
                    actual_cost=None,
                    cost_status="unavailable",
                    result=budget_result,
                )
            else:
                assert ownership.delegation_id is not None
                await uow.shared_budget.settle_allocation(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=ownership.budget_owner_run_id,
                    delegation_id=ownership.delegation_id,
                    usage_call_id=usage_call_id,
                    actual_tokens=None,
                    actual_cost=None,
                    cost_status="unavailable",
                    result=budget_result,
                )
            await uow.commit()
        if record.route_chain_state != state:
            raise RuntimeError("route-chain cleanup review did not persist exact state")
        raise ModelProviderInvocationError(
            "model.provider_side_effect_unknown",
            provider_called=True,
            attempt_count=global_attempt,
            latency_ms=response.latency_ms,
            failure_domain="runtime",
        ) from None

    @staticmethod
    def _global_response_attempt(
        response: ModelResponse,
        global_attempt: int,
    ) -> ModelAttemptEvidence:
        """把候选内 attempt 1 投影为全链连续 ordinal。"""

        if len(response.attempts) != 1:
            raise ValueError("chain provider response must contain exactly one attempt")
        return response.attempts[0].model_copy(update={"attempt": global_attempt})

    def _chain_final_evidence(
        self,
        *,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        response: ModelResponse,
    ) -> ModelUsageEvidence:
        """投影 completed evidence；route 必须命中 selected/evidence ordinal。"""

        candidate = chain.candidates[state.evidence_route_ordinal - 1]
        attempts = self._chain_attempt_evidence(chain=chain, state=state, response=response)
        decision = self._safe_decision(
            candidate.route.decision.to_payload(),
            response.decision.to_payload(),
            {
                "route": self._route_evidence(candidate.route),
                "provider_called": True,
                "attempts": attempts,
                "budget_charge": self._chain_budget_charge(attempts),
            },
        )
        decision["route_chain"] = {
            "schema_version": "model-route-chain-evidence-v1",
            "identity": chain.model_dump(mode="json"),
            "state": state.to_payload(),
        }
        return self._started_evidence(
            context=context,
            provider=candidate.provider,
            model=candidate.model,
            input_tokens=response.token_usage.get("input_tokens"),
            output_tokens=response.token_usage.get("output_tokens"),
            cost_usd=response.cost_usd,
            cost_status=response.cost_status,
            latency_ms=response.latency_ms,
            decision=decision,
        )

    @staticmethod
    def _route_chain_exhausted_detail(
        *,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
    ) -> ModelRouteChainExhaustedDetail:
        """把所有终态候选投影为连续、去敏且可重放的 exhaustion causes。"""

        causes: list[dict[str, object]] = []
        for candidate_state in state.candidates:
            candidate = chain.candidates[candidate_state.ordinal - 1]
            if candidate_state.state == "static_ineligible":
                cause = candidate.static_ineligible_cause or "catalog"
            elif candidate_state.state == "budget_ineligible":
                cause = candidate_state.reason
            elif candidate_state.state == "not_started":
                cause = "not_started_failure"
            else:
                raise ValueError("route-chain exhaustion contains a non-terminal candidate")
            causes.append({"ordinal": candidate_state.ordinal, "cause": cause})
        return ModelRouteChainExhaustedDetail.model_validate(
            {
                "schema_version": "model-route-chain-exhausted-v1",
                "chain_id": chain.chain_id,
                "causes": causes,
            }
        )

    def _chain_failure_evidence(
        self,
        *,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        error_code: str,
    ) -> ModelUsageEvidence:
        """安全耗尽 evidence 以零 actual 结算全部 proven attempts。"""

        candidate = chain.candidates[state.evidence_route_ordinal - 1]
        attempts = self._chain_attempt_evidence(chain=chain, state=state, response=None)
        cost_enabled = any(item.route.trusted_cost_bound is not None for item in chain.candidates)
        provider_called = any(item.request_sent for item in state.attempt_lifecycle)
        decision = self._safe_decision(
            candidate.route.decision.to_payload(),
            {
                "route": self._route_evidence(candidate.route),
                "provider_called": provider_called,
                "attempts": attempts,
                "budget_charge": self._chain_budget_charge(attempts),
                "error_code": error_code,
            },
        )
        decision["route_chain"] = {
            "schema_version": "model-route-chain-evidence-v1",
            "identity": chain.model_dump(mode="json"),
            "state": state.to_payload(),
        }
        if error_code == "model.route_chain_exhausted":
            decision["route_chain_exhausted"] = self._route_chain_exhausted_detail(
                chain=chain,
                state=state,
            ).to_payload()
        return self._started_evidence(
            context=context,
            provider=candidate.provider,
            model=candidate.model,
            input_tokens=0 if provider_called else None,
            output_tokens=0 if provider_called else None,
            cost_usd=0.0 if provider_called and cost_enabled else None,
            cost_status="reported" if provider_called and cost_enabled else "unavailable",
            decision=decision,
        )

    @staticmethod
    def _chain_attempt_evidence(
        *,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        response: ModelResponse | None,
    ) -> list[dict[str, object]]:
        """逐 lifecycle 投影 chain-only exact attempt 扩展字段。"""

        final_attempt = None if response is None else response.attempts[0]
        proofs = {
            proof.attempt: proof
            for candidate in state.candidates
            for proof in candidate.not_started_proofs
        }
        values: list[dict[str, object]] = []
        for lifecycle in state.attempt_lifecycle:
            candidate = chain.candidates[lifecycle.candidate_ordinal - 1]
            candidate_state = state.candidates[lifecycle.candidate_ordinal - 1]
            proof = proofs.get(lifecycle.attempt)
            settled = lifecycle.lifecycle_state == "settled"
            cancelled = settled and candidate_state.state == "cancelled"
            source = final_attempt if settled else None
            values.append(
                {
                    "attempt": lifecycle.attempt,
                    "candidate_ordinal": lifecycle.candidate_ordinal,
                    "deployment_id": candidate.deployment_id,
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "outcome": (
                        "cancelled"
                        if cancelled
                        else "completed"
                        if settled
                        else "unknown"
                        if lifecycle.lifecycle_state == "unknown"
                        else "retryable_status"
                        if proof is not None and proof.reason == "trusted_business_not_started"
                        else "failed"
                    ),
                    "side_effect_state": (
                        "started"
                        if lifecycle.side_effect_state == "result_committed"
                        else lifecycle.side_effect_state
                    ),
                    "request_sent": lifecycle.request_sent,
                    "http_response_observed": lifecycle.http_response_observed,
                    "http_status": lifecycle.http_status,
                    "response_identity_observed": lifecycle.response_identity_observed,
                    "usage_observed": lifecycle.usage_observed,
                    "text_observed": lifecycle.text_observed,
                    "delta_observed": lifecycle.delta_observed,
                    "completion_observed": lifecycle.completion_observed,
                    "not_started_reason": None if proof is None else proof.reason,
                    "not_started_proof_digest": (None if proof is None else proof.proof_digest),
                    "endpoint_policy_digest": candidate.endpoint_policy_digest,
                    "classifier_ref": None if proof is None else proof.classifier_ref,
                    "classifier_version": None if proof is None else proof.classifier_version,
                    "retry_after_ms": None,
                    "input_tokens": None if source is None else source.input_tokens,
                    "output_tokens": None if source is None else source.output_tokens,
                    "cost_usd": None if source is None else source.cost_usd,
                    "cost_status": "unavailable" if source is None else source.cost_status,
                    "budget_charge_tokens": (
                        0
                        if proof is not None
                        else None
                        if source is None
                        else (source.input_tokens or 0) + (source.output_tokens or 0)
                    ),
                    "budget_charge_cost_usd": (
                        0.0
                        if proof is not None and candidate.route.trusted_cost_bound is not None
                        else None
                        if source is None
                        else source.cost_usd
                    ),
                    "latency_ms": 0 if source is None else source.latency_ms,
                    "error_code": (
                        "model.invocation_cancelled"
                        if cancelled
                        else None
                        if settled
                        else "model.provider_retry_exhausted"
                    ),
                }
            )
        return values

    @staticmethod
    def _chain_budget_charge(attempts: list[dict[str, object]]) -> dict[str, object]:
        """proof attempts 收敛为零，只有 settled attempt 贡献 actual。"""

        unresolved = [
            cast(int, item["attempt"]) for item in attempts if item["budget_charge_tokens"] is None
        ]
        return {
            "charged_tokens": (
                None
                if unresolved
                else sum(cast(int, item["budget_charge_tokens"]) for item in attempts)
            ),
            "charged_cost_usd": (
                None
                if unresolved
                else sum(
                    cast(float, item["budget_charge_cost_usd"])
                    for item in attempts
                    if item["budget_charge_cost_usd"] is not None
                )
            ),
            "charge_status": "unknown" if unresolved else "actual",
            "unresolved_attempts": unresolved,
        }


__all__ = ["_ChainEvidenceMixin"]
