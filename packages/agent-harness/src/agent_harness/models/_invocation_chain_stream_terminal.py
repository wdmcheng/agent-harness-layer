"""流式 route-chain 的安全耗尽终态协调。"""

from __future__ import annotations

from typing import Never, cast

from agent_harness.models._invocation_chain_base import ChainRuntimeBase
from agent_harness.models._invocation_chain_stream_support import with_stream_usage_identity
from agent_harness.models._route_chain_state import transfer_route_reservation
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import ModelProviderInvocationError
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.shared_budget import BudgetOperationOwnership


class ChainStreamingTerminalMixin(ChainRuntimeBase):
    """只负责把最后一个可信 not-started 候选收敛为耗尽终态。"""

    async def _raise_stream_chain_exhausted(
        self,
        *,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        candidate_ordinal: int,
        usage_call_id: str,
        cost_enabled: bool,
        ownership: BudgetOperationOwnership | None,
    ) -> Never:
        proof_state = state
        state = transfer_route_reservation(
            proof_state,
            chain=chain,
            to_ordinal=None,
            reason=cast(str, state.candidates[candidate_ordinal - 1].reason),
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
        evidence = with_stream_usage_identity(evidence, safe_decision=self._safe_decision)
        await self._finalize(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome="failed",
            error_code="model.route_chain_exhausted",
            ownership=ownership,
            response=None,
        )
        raise ModelProviderInvocationError(
            "model.route_chain_exhausted",
            provider_called=any(item.request_sent for item in state.attempt_lifecycle),
            attempt_count=len(state.attempt_lifecycle),
            detail=self._route_chain_exhausted_detail(chain=chain, state=state),
        )


__all__ = ["ChainStreamingTerminalMixin"]
