"""普通 completion 候选执行与结算协调。"""

from __future__ import annotations

import asyncio
from typing import cast

from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain_base import (
    ChainRuntimeBase,
    ModelApprovalGrantLike,
)
from agent_harness.models._route_chain_state import (
    append_route_attempt_started,
    close_route_attempt,
    prove_route_attempt_not_started,
    transfer_route_reservation,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
)
from agent_harness.models.providers import (
    ModelRequest,
    ModelResponse,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.models.usage_events import UsageEvidenceLifecycle


class _ChainCompletionMixin(ChainRuntimeBase):
    async def _complete_chain(
        self,
        request: ModelRequest,
        *,
        chain: ModelRouteChainPlan,
        context: UsageEvidenceContext,
        usage_call_id: str,
        operation_identity_digest: str,
        soft_approved: bool,
        actor: IdentityContext | None,
        approved_grant: ModelApprovalGrantLike | None = None,
    ) -> ModelResponse:
        """执行显式 completion chain，并保持一笔 usage/claim/outbox identity。"""

        settlement, state, policy_outcome = await self._start_initial_chain(
            request=request,
            chain=chain,
            context=context,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            soft_approved=soft_approved,
            actor=actor,
            stream=False,
        )
        cost_enabled = any(
            candidate.route.trusted_cost_bound is not None for candidate in chain.candidates
        )
        if policy_outcome.decision == "deny":
            await self._finalize_initial_chain_policy_denied(
                context=context,
                chain=chain,
                state=state,
                usage_call_id=usage_call_id,
                settlement=settlement,
                stream=False,
            )
        if policy_outcome.decision == "require_approval":
            from agent_harness.models._invocation_execution import ModelApprovalRequired

            if policy_outcome.request is None:
                raise RuntimeError("approval policy outcome omitted its request")
            raise ModelApprovalRequired(policy_outcome.request)
        if state.waiting_approval_ordinal is not None:
            if approved_grant is None:
                return await self._resume_existing_settlement(
                    claim=settlement.usage,
                    usage_call_id=usage_call_id,
                )
            waiting = state.candidates[state.waiting_approval_ordinal - 1]
            request_digest = waiting.approval_request_binding_digest
            if request_digest is None:
                raise ValueError("route-chain approval request binding is missing")
            state = await self._activate_or_skip_approved_route(
                request=request,
                context=context,
                chain=chain,
                state=state,
                approved_grant=approved_grant,
                request_binding_digest=request_digest,
                usage_call_id=usage_call_id,
                operation_identity_digest=operation_identity_digest,
                actor=actor,
                cost_enabled=cost_enabled,
                settlement=settlement,
                stream=False,
            )
        elif approved_grant is not None and state.active_ordinal is not None:
            active = state.candidates[state.active_ordinal - 1]
            if active.approval_request_binding_digest is not None:
                expected_grant = self._route_approval_grant_digest(
                    approved_grant=approved_grant,
                    request_binding_digest=active.approval_request_binding_digest,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                )
                if active.approval_grant_binding_digest != expected_grant:
                    raise ValueError("model approval grant does not match activated route")
        elif not settlement.usage.created and not settlement.safe_to_start:
            return await self._resume_existing_settlement(
                claim=settlement.usage,
                usage_call_id=usage_call_id,
            )
        started_evidence = settlement.started_evidence
        if started_evidence is None:
            raise RuntimeError("route-chain settlement omitted started evidence")
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started_evidence,
            usage_call_id=usage_call_id,
        )
        started_event = await lifecycle.publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started_event)
        loop = asyncio.get_running_loop()

        start_ordinal = state.active_ordinal
        if start_ordinal is None:
            raise RuntimeError("route chain has no active candidate")
        for candidate in chain.candidates[start_ordinal - 1 :]:
            if state.active_ordinal != candidate.ordinal:
                continue
            candidate_deadline = loop.time() + candidate.route.total_timeout_ms / 1000
            for _route_attempt in range(1, candidate.route.max_attempts + 1):
                state = append_route_attempt_started(
                    state,
                    chain=chain,
                    candidate_ordinal=candidate.ordinal,
                )
                state = await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=state,
                    method="append_model_route_attempt_started",
                )
                prepared = None
                response = None
                try:
                    attempt_plan = candidate.route.model_copy(update={"max_attempts": 1})
                    routed_request = request.model_copy(
                        update={
                            "deployment_id": candidate.deployment_id,
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "route_refs": None,
                            "max_output_tokens": candidate.route.output_token_cap,
                        }
                    )
                    async with asyncio.timeout_at(candidate_deadline):
                        prepared = await self._router.prepare(
                            routed_request,
                            plan=attempt_plan,
                        )
                        response = self._router.normalize_response(
                            await prepared.send(),
                            plan=attempt_plan,
                        )
                    response = ModelResponse.model_validate(response.model_dump(mode="python"))
                except asyncio.CancelledError:
                    if prepared is not None:
                        try:
                            await prepared.aclose()
                        except Exception:  # noqa: BLE001 - 后置本地清理不能覆盖稳定取消错误
                            pass
                        finally:
                            prepared = None
                    await self._raise_cancelled_route_attempt_unknown(
                        context=context,
                        usage_call_id=usage_call_id,
                        state=state,
                        candidate_ordinal=candidate.ordinal,
                        ownership=settlement.ownership,
                        request_sent=False,
                    )
                except Exception as exc:
                    facts = self._trusted_not_started_facts(exc, candidate=candidate)
                    if facts is not None:
                        proof_state = prove_route_attempt_not_started(
                            state,
                            candidate_ordinal=candidate.ordinal,
                            facts=facts,
                        )
                        if _route_attempt < candidate.route.max_attempts:
                            state = await self._persist_route_chain_state(
                                context=context,
                                usage_call_id=usage_call_id,
                                state=proof_state,
                                method="append_model_route_not_started_proof",
                            )
                            continue
                        # 最后一次 proof 与后继 reservation/终态必须同事务提交，
                        # 避免 crash 后暴露“仍 active 的已关闭候选”。
                        state = proof_state
                        break
                    observations = self._route_attempt_observations(exc)
                    state = close_route_attempt(
                        state,
                        candidate_ordinal=candidate.ordinal,
                        lifecycle_state="unknown",
                        response_observed=observations.response_observed,
                        request_sent=observations.request_sent,
                        usage_observed=observations.usage_observed,
                        text_observed=observations.text_observed,
                        delta_observed=observations.delta_observed,
                        completion_observed=observations.completion_observed,
                    )
                    await self._persist_route_chain_state(
                        context=context,
                        usage_call_id=usage_call_id,
                        state=state,
                        method="close_model_route_attempt",
                    )
                    if settlement.ownership is None:
                        raise RuntimeError(
                            "route-chain settlement omitted budget ownership"
                        ) from None
                    async with self._storage.uow() as uow:
                        await uow.shared_budget.recover_unknown_started(
                            tenant_id=context.tenant_id,
                            budget_owner_run_id=settlement.ownership.budget_owner_run_id,
                        )
                        await uow.commit()
                    if prepared is not None:
                        try:
                            await prepared.aclose()
                        except Exception:  # noqa: BLE001 - 后置清理不得覆盖稳定unknown
                            pass
                        finally:
                            prepared = None
                    raise ModelProviderInvocationError(
                        "model.provider_side_effect_unknown",
                        provider_called=self._route_chain_provider_called(state),
                        attempt_count=len(state.attempt_lifecycle),
                    ) from None
                finally:
                    if prepared is not None:
                        try:
                            await prepared.aclose()
                        except Exception:  # noqa: BLE001 - cleanup不确定性由耐久attempt-review封闭
                            if response is None:
                                raise
                            await self._raise_cleanup_route_attempt_unknown(
                                context=context,
                                usage_call_id=usage_call_id,
                                state=state,
                                candidate_ordinal=candidate.ordinal,
                                ownership=settlement.ownership,
                                response=response,
                                delta_observed=False,
                            )

                global_attempt = len(state.attempt_lifecycle)
                final_attempt = self._global_response_attempt(response, global_attempt)
                response = response.model_copy(update={"attempts": [final_attempt]})
                state = close_route_attempt(
                    state,
                    candidate_ordinal=candidate.ordinal,
                    lifecycle_state="settled",
                    response_observed=True,
                )
                evidence = self._chain_final_evidence(
                    context=context,
                    chain=chain,
                    state=state,
                    response=response,
                )
                await self._finalize(
                    evidence=evidence,
                    usage_call_id=usage_call_id,
                    outcome="completed",
                    error_code=None,
                    ownership=settlement.ownership,
                    response=response,
                )
                return response

            # 当前候选的全部 actual attempts 都已有 proof，才允许预约后继。
            if candidate.ordinal < chain.candidate_count:
                state = await self._advance_chain_successor(
                    request=request,
                    context=context,
                    chain=chain,
                    state=state,
                    current_ordinal=candidate.ordinal,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                    soft_approved=soft_approved and approved_grant is None,
                    actor=actor,
                    cost_enabled=cost_enabled,
                    settlement=settlement,
                    stream=False,
                )
                continue

            proof_state = state
            state = transfer_route_reservation(
                proof_state,
                chain=chain,
                to_ordinal=None,
                reason=cast(str, state.candidates[candidate.ordinal - 1].reason),
                cost_enabled=cost_enabled,
            )
            state = await self._persist_route_chain_state(
                context=context,
                usage_call_id=usage_call_id,
                state=state,
                method="prove_and_transfer_model_route_reservation",
                proof_state=proof_state,
            )
            evidence = self._chain_failure_evidence(
                context=context,
                chain=chain,
                state=state,
                error_code="model.route_chain_exhausted",
            )
            await self._finalize(
                evidence=evidence,
                usage_call_id=usage_call_id,
                outcome="failed",
                error_code="model.route_chain_exhausted",
                ownership=settlement.ownership,
                response=None,
            )
            raise ModelProviderInvocationError(
                "model.route_chain_exhausted",
                provider_called=any(item.request_sent for item in state.attempt_lifecycle),
                attempt_count=len(state.attempt_lifecycle),
                detail=self._route_chain_exhausted_detail(chain=chain, state=state),
            )
        raise RuntimeError("route chain ended without a terminal outcome")


__all__ = ["_ChainCompletionMixin"]
