"""初始候选扫描、后继推进与终态协调。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain_base import (
    ChainPolicyOutcome,
    ChainRuntimeBase,
)
from agent_harness.models._route_chain_state import (
    mark_route_budget_ineligible,
    mark_route_static_ineligible,
    terminate_route_policy_denied,
    transfer_route_reservation,
    wait_for_route_approval,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
)
from agent_harness.models.providers import (
    ModelRequest,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.shared_budget import (
    BudgetReservationRejected,
)

if TYPE_CHECKING:
    from agent_harness.models._settlement_contracts import SettlementStart


class _ChainRoutingMixin(ChainRuntimeBase):
    async def _start_initial_chain(
        self,
        *,
        request: ModelRequest,
        chain: ModelRouteChainPlan,
        context: UsageEvidenceContext,
        usage_call_id: str,
        operation_identity_digest: str,
        soft_approved: bool,
        actor: IdentityContext | None,
        stream: bool,
    ) -> tuple[SettlementStart, ModelRouteChainState, ChainPolicyOutcome]:
        """按 ordinal 完成初始 policy/soft/balance 扫描，并只耐久首个最终决定。"""

        skips: dict[int, Literal["static_ineligible", "soft_budget", "balance"]] = {}
        for candidate in chain.candidates:
            if candidate.static_ineligible_cause is not None:
                skips[candidate.ordinal] = "static_ineligible"
                continue
            policy = ChainPolicyOutcome(decision="allow")
            if not soft_approved:
                policy = await self._require_chain_policy_allow(
                    request=request,
                    candidate=candidate,
                    context=context,
                    actor=actor,
                    chain=chain,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                )
            if policy.decision == "allow" and candidate.route.approval_kind == "soft_budget":
                skips[candidate.ordinal] = "soft_budget"
                continue
            if policy.decision == "deny" and not skips:
                # 首个实际评估候选在任何预算决定前被拒绝时，不创建 coordination
                # claim；它与 legacy policy deny 一样停在授权边界外。
                raise ModelProviderInvocationError("model.policy_denied")
            stream_identity: dict[str, object] = {
                "usage_event_identity": {"ref": "stream-usage", "version": "v1"}
            }
            evidence_seed = self._started_evidence(
                context=context,
                provider=candidate.provider,
                model=candidate.model,
                decision=self._safe_decision(
                    candidate.route.decision.to_payload(),
                    {"provider_called": False},
                    *((stream_identity,) if stream else ()),
                ),
            )
            try:
                settlement = await self._start_chain_settlement(
                    evidence=evidence_seed,
                    usage_call_id=usage_call_id,
                    request=request,
                    chain=chain,
                    operation_identity_digest=operation_identity_digest,
                    waiting_approval_ordinal=(
                        candidate.ordinal if policy.decision == "require_approval" else None
                    ),
                    approval_request_binding_digest=(
                        policy.request_binding_digest
                        if policy.decision == "require_approval"
                        else None
                    ),
                    denied_ordinal=(candidate.ordinal if policy.decision == "deny" else None),
                    initial_active_ordinal=candidate.ordinal,
                    initial_skips=dict(skips),
                    stream=stream,
                )
            except BudgetReservationRejected as exc:
                if policy.decision != "allow" or exc.reason != "balance_insufficient":
                    raise
                skips[candidate.ordinal] = "balance"
                continue
            state = await self._load_route_chain_state(context, usage_call_id)
            return settlement, state, policy

        last = chain.candidates[-1]
        stream_identity = {"usage_event_identity": {"ref": "stream-usage", "version": "v1"}}
        evidence_seed = self._started_evidence(
            context=context,
            provider=last.provider,
            model=last.model,
            decision=self._safe_decision(
                last.route.decision.to_payload(),
                {"provider_called": False},
                *((stream_identity,) if stream else ()),
            ),
        )
        settlement = await self._start_chain_settlement(
            evidence=evidence_seed,
            usage_call_id=usage_call_id,
            request=request,
            chain=chain,
            operation_identity_digest=operation_identity_digest,
            initial_skips=skips,
            initial_exhausted=True,
            stream=stream,
        )
        state = await self._load_route_chain_state(context, usage_call_id)
        await self._finalize_chain_terminal(
            context=context,
            chain=chain,
            state=state,
            usage_call_id=usage_call_id,
            settlement=settlement,
            error_code="model.route_chain_exhausted",
            stream=stream,
            publish_started=True,
        )
        raise AssertionError("route-chain terminal finalizer must raise")

    async def _finalize_initial_chain_policy_denied(
        self,
        *,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        usage_call_id: str,
        settlement: SettlementStart,
        stream: bool,
    ) -> None:
        """发布零 provider 的调用生命周期，并结算初始候选 policy deny。"""

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
        evidence = self._chain_failure_evidence(
            context=context,
            chain=chain,
            state=state,
            error_code="model.policy_denied",
        )
        if stream:
            evidence = self._stream_chain_evidence(evidence)
        await self._finalize(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome="failed",
            error_code="model.policy_denied",
            ownership=settlement.ownership,
            response=None,
        )
        raise ModelProviderInvocationError("model.policy_denied")

    async def _advance_chain_successor(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        current_ordinal: int,
        usage_call_id: str,
        operation_identity_digest: str,
        soft_approved: bool,
        actor: IdentityContext | None,
        cost_enabled: bool,
        settlement: SettlementStart,
        stream: bool,
    ) -> ModelRouteChainState:
        """依 ordinal 重新授权后继，并把普通 balance skip 收敛进最终 durable state。"""

        source_reason = cast(str, state.candidates[current_ordinal - 1].reason)
        scan_state = state
        for successor in chain.candidates[current_ordinal:]:
            if successor.static_ineligible_cause is not None:
                scan_state = mark_route_static_ineligible(
                    scan_state,
                    candidate_ordinal=successor.ordinal,
                )
                continue
            policy = ChainPolicyOutcome(decision="allow")
            if not soft_approved:
                policy = await self._require_chain_policy_allow(
                    request=request,
                    candidate=successor,
                    context=context,
                    actor=actor,
                    chain=chain,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                )
            if policy.decision == "deny":
                denied = terminate_route_policy_denied(
                    scan_state,
                    target_ordinal=successor.ordinal,
                )
                denied = await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=denied,
                    method="prove_and_transfer_model_route_reservation",
                    proof_state=state,
                )
                evidence = self._chain_failure_evidence(
                    context=context,
                    chain=chain,
                    state=denied,
                    error_code="model.policy_denied",
                )
                if stream:
                    evidence = self._stream_chain_evidence(evidence)
                await self._finalize(
                    evidence=evidence,
                    usage_call_id=usage_call_id,
                    outcome="failed",
                    error_code="model.policy_denied",
                    ownership=settlement.ownership,
                    response=None,
                )
                raise ModelProviderInvocationError("model.policy_denied")
            if policy.decision == "require_approval":
                if policy.request is None or policy.request_binding_digest is None:
                    raise RuntimeError("approval policy outcome omitted its binding")
                waiting = wait_for_route_approval(
                    scan_state,
                    target_ordinal=successor.ordinal,
                    approval_request_binding_digest=policy.request_binding_digest,
                )
                await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=waiting,
                    method="prove_and_transfer_model_route_reservation",
                    proof_state=state,
                )
                from agent_harness.models._invocation_execution import ModelApprovalRequired

                raise ModelApprovalRequired(policy.request)

            if successor.route.approval_kind == "soft_budget":
                scan_state = mark_route_budget_ineligible(
                    scan_state,
                    candidate_ordinal=successor.ordinal,
                    reason="soft_budget",
                )
                continue

            candidate_state = transfer_route_reservation(
                scan_state,
                chain=chain,
                to_ordinal=successor.ordinal,
                reason=source_reason,
                cost_enabled=cost_enabled,
            )
            try:
                return await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=candidate_state,
                    method="prove_and_transfer_model_route_reservation",
                    proof_state=state,
                )
            except BudgetReservationRejected as exc:
                if exc.reason != "balance_insufficient":
                    raise
                scan_state = mark_route_budget_ineligible(
                    scan_state,
                    candidate_ordinal=successor.ordinal,
                )

        exhausted = transfer_route_reservation(
            scan_state,
            chain=chain,
            to_ordinal=None,
            reason=source_reason,
            cost_enabled=cost_enabled,
        )
        exhausted = await self._persist_route_chain_state(
            context=context,
            usage_call_id=usage_call_id,
            state=exhausted,
            method="prove_and_transfer_model_route_reservation",
            proof_state=state,
        )
        evidence = self._chain_failure_evidence(
            context=context,
            chain=chain,
            state=exhausted,
            error_code="model.route_chain_exhausted",
        )
        if stream:
            evidence = self._stream_chain_evidence(evidence)
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
            provider_called=any(item.request_sent for item in exhausted.attempt_lifecycle),
            attempt_count=len(exhausted.attempt_lifecycle),
            detail=self._route_chain_exhausted_detail(chain=chain, state=exhausted),
        )

    async def _finalize_chain_terminal(
        self,
        *,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        usage_call_id: str,
        settlement: SettlementStart,
        error_code: Literal["model.policy_denied", "model.route_chain_exhausted"],
        stream: bool,
        publish_started: bool,
    ) -> None:
        """以同一 usage claim/outbox 结算 policy deny 或安全耗尽并抛稳定错误。"""

        if publish_started:
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
        evidence = self._chain_failure_evidence(
            context=context,
            chain=chain,
            state=state,
            error_code=error_code,
        )
        if stream:
            evidence = self._stream_chain_evidence(evidence)
        await self._finalize(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome="failed",
            error_code=error_code,
            ownership=settlement.ownership,
            response=None,
        )
        if error_code == "model.policy_denied":
            raise ModelProviderInvocationError(error_code)
        raise ModelProviderInvocationError(
            error_code,
            provider_called=any(item.request_sent for item in state.attempt_lifecycle),
            attempt_count=len(state.attempt_lifecycle),
            detail=self._route_chain_exhausted_detail(chain=chain, state=state),
        )


__all__ = ["_ChainRoutingMixin"]
