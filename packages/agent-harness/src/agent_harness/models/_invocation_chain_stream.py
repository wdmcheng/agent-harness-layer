"""流式候选执行、首 delta 围栏与完成协调。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain_base import (
    ModelApprovalGrantLike,
)
from agent_harness.models._invocation_chain_stream_support import (
    validate_chain_stream_routes,
    with_stream_usage_identity,
)
from agent_harness.models._invocation_chain_stream_terminal import (
    ChainStreamingTerminalMixin,
)
from agent_harness.models._route_chain_state import (
    append_route_attempt_started,
    close_route_attempt,
    mark_route_delta_observed,
    prove_route_attempt_not_started,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
)
from agent_harness.models._streaming_consumption import consume_prepared_stream
from agent_harness.models._streaming_events import (
    persist_completed_and_final,
    publish_persisted_stream,
)
from agent_harness.models._streaming_settlement import has_trustworthy_stopped_usage
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
    ModelStreamCloseResult,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.models.usage_events import UsageEvidenceLifecycle

if TYPE_CHECKING:
    from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork


class _ChainStreamingMixin(ChainStreamingTerminalMixin):
    async def _stream_chain(
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
        """在首个 delta 前允许可信切换；观察 delta 后永久关闭后继候选。"""

        validate_chain_stream_routes(self._router, request, chain)
        settlement, state, policy_outcome = await self._start_initial_chain(
            request=request,
            chain=chain,
            context=context,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            soft_approved=soft_approved,
            actor=actor,
            stream=True,
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
                stream=True,
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
                stream=True,
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
            raise RuntimeError("route-chain stream settlement omitted started evidence")
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started_evidence,
            usage_call_id=usage_call_id,
        )
        started_event = await lifecycle.publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started_event)
        helpers = self._streaming_runtime()
        loop = asyncio.get_running_loop()

        start_ordinal = state.active_ordinal
        if start_ordinal is None:
            raise RuntimeError("stream route chain has no active candidate")
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
                global_attempt = len(state.attempt_lifecycle)
                prepared = None
                response = None
                chunks: list[str] = []
                fenced_state = None

                async def fence_first_delta(
                    candidate_ordinal: int = candidate.ordinal,
                ) -> None:
                    nonlocal fenced_state, state
                    state = mark_route_delta_observed(
                        state,
                        candidate_ordinal=candidate_ordinal,
                    )
                    fenced_state = state

                async def persist_first_delta_fence(uow: SQLAlchemyUnitOfWork) -> None:
                    """把内存围栏与首个公开 delta intent 原子写入同一 owner UoW。"""

                    if fenced_state is None:  # noqa: B023 - 本轮首次 delta 的可变围栏
                        raise RuntimeError("first delta fence was not prepared")
                    shared_budget = uow.shared_budget
                    record = await shared_budget.mark_model_route_delta_observed(
                        tenant_id=context.tenant_id,
                        run_id=context.run_id,
                        usage_call_id=usage_call_id,
                        state=fenced_state,  # noqa: B023
                    )
                    if record.route_chain_state != fenced_state:  # noqa: B023
                        raise RuntimeError("route-chain delta fence did not persist exact state")

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
                        prepared = await self._router.prepare_stream(
                            routed_request,
                            plan=attempt_plan,
                        )
                        response = await consume_prepared_stream(
                            helpers,
                            prepared=prepared,
                            context=context,
                            usage_call_id=usage_call_id,
                            ownership=settlement.ownership,
                            plan=attempt_plan,
                            chunks=chunks,
                            attempt=global_attempt,
                            mark_side_effect_started=False,
                            on_first_delta=fence_first_delta,
                            persist_first_delta_uow=persist_first_delta_fence,
                        )
                except asyncio.CancelledError:
                    close_result = ModelStreamCloseResult(state="not_started")
                    if prepared is not None:
                        try:
                            close_result = await prepared.aclose()
                        except Exception:  # noqa: BLE001 - close失败归入稳定unknown，不泄漏本地异常
                            close_result = ModelStreamCloseResult(state="unknown")
                        finally:
                            prepared = None
                    durable_delta_pending = state.delta_fenced
                    if has_trustworthy_stopped_usage(
                        plan=candidate.route,
                        close_result=close_result,
                    ):
                        async with self._storage.uow() as uow:
                            group = await uow.evidence_outbox.ordered_group(
                                group_id=f"model-stream:{usage_call_id}"
                            )
                            durable_delta_pending = durable_delta_pending or any(
                                item.state == "result_persisted" for item in group
                            )
                    if not durable_delta_pending and has_trustworthy_stopped_usage(
                        plan=candidate.route,
                        close_result=close_result,
                    ):
                        usage = close_result.usage
                        assert usage is not None
                        state = close_route_attempt(
                            state,
                            candidate_ordinal=candidate.ordinal,
                            lifecycle_state="settled",
                            response_observed=False,
                            request_sent=True,
                            usage_observed=True,
                            text_observed=False,
                            completion_observed=False,
                            terminal_outcome="cancelled",
                        )
                        attempt = ModelAttemptEvidence(
                            attempt=global_attempt,
                            side_effect_state="started",
                            outcome="cancelled",
                            completion_observed=False,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cost_usd=usage.cost_usd,
                            cost_status=usage.cost_status,
                            latency_ms=usage.latency_ms,
                            error_code="model.invocation_cancelled",
                        )
                        cancelled_response = ModelResponse(
                            provider=candidate.provider,
                            model=candidate.model,
                            output_text="",
                            decision=candidate.route.decision,
                            token_usage={
                                "input_tokens": cast(int, usage.input_tokens),
                                "output_tokens": cast(int, usage.output_tokens),
                            },
                            latency_ms=usage.latency_ms,
                            cost_usd=usage.cost_usd,
                            cost_status=usage.cost_status,
                            attempts=[attempt],
                        )
                        evidence = self._chain_final_evidence(
                            context=context,
                            chain=chain,
                            state=state,
                            response=cancelled_response,
                        )
                        evidence = with_stream_usage_identity(
                            evidence, safe_decision=self._safe_decision
                        )
                        async with self._storage.uow() as uow:
                            await uow.evidence_outbox.cancel_unused_stream(
                                tenant_id=context.tenant_id,
                                run_id=context.run_id,
                                usage_call_id=usage_call_id,
                                used_delta_count=0,
                                keep_completed=False,
                            )
                            await self._persist_final_in_uow(
                                uow=uow,
                                evidence=evidence,
                                usage_call_id=usage_call_id,
                                outcome="cancelled",
                                error_code="model.invocation_cancelled",
                                ownership=settlement.ownership,
                                response=None,
                            )
                            await uow.commit()
                        await self._publish_final(
                            evidence=evidence,
                            usage_call_id=usage_call_id,
                            outcome="cancelled",
                            error_code="model.invocation_cancelled",
                        )
                        raise ModelProviderInvocationError(
                            "model.invocation_cancelled",
                            provider_called=True,
                            attempt_count=len(state.attempt_lifecycle),
                            latency_ms=usage.latency_ms,
                            failure_domain="runtime",
                        ) from None
                    usage = close_result.usage
                    provider_observed = close_result.state == "stopped" or usage is not None
                    await self._raise_cancelled_route_attempt_unknown(
                        context=context,
                        usage_call_id=usage_call_id,
                        state=state,
                        candidate_ordinal=candidate.ordinal,
                        ownership=settlement.ownership,
                        request_sent=provider_observed,
                        usage_observed=usage is not None,
                        delta_observed=durable_delta_pending,
                        completion_observed=(False if provider_observed else None),
                    )
                except Exception as exc:
                    facts = (
                        None
                        if state.delta_fenced
                        else self._trusted_not_started_facts(exc, candidate=candidate)
                    )
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
                    observations = self._route_attempt_observations(
                        exc,
                        delta_observed=state.delta_fenced,
                    )
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
                    state = await self._persist_route_chain_state(
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
                    if not hasattr(exc, "code") and not isinstance(exc, TimeoutError):
                        raise
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
                                delta_observed=bool(chunks),
                            )

                if response is None:
                    raise RuntimeError("route-chain stream completed without a response")
                final_attempt = self._global_response_attempt(response, global_attempt)
                response = response.model_copy(update={"attempts": [final_attempt]})
                state = close_route_attempt(
                    state,
                    candidate_ordinal=candidate.ordinal,
                    lifecycle_state="settled",
                    response_observed=True,
                    delta_observed=bool(chunks),
                )
                evidence = self._chain_final_evidence(
                    context=context,
                    chain=chain,
                    state=state,
                    response=response,
                )
                evidence = with_stream_usage_identity(evidence, safe_decision=self._safe_decision)
                completed_intent = await persist_completed_and_final(
                    helpers,
                    context=context,
                    usage_call_id=usage_call_id,
                    chunks=chunks,
                    evidence=evidence,
                    outcome="completed",
                    error_code=None,
                    ownership=settlement.ownership,
                    response=response,
                    attempt=global_attempt,
                )
                await publish_persisted_stream(helpers, completed_intent)
                await self._publish_final(
                    evidence=evidence,
                    usage_call_id=usage_call_id,
                    outcome="completed",
                    error_code=None,
                )
                return response

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
                    stream=True,
                )
                continue

            await self._raise_stream_chain_exhausted(
                context=context,
                chain=chain,
                state=state,
                candidate_ordinal=candidate.ordinal,
                usage_call_id=usage_call_id,
                cost_enabled=cost_enabled,
                ownership=settlement.ownership,
            )
        raise RuntimeError("stream route chain ended without a terminal outcome")


__all__ = ["_ChainStreamingMixin"]
