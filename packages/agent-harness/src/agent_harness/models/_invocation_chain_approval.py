"""候选级策略、审批激活与余额锚点协调。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, cast

from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain_base import (
    ChainPolicyOutcome,
    ChainRuntimeBase,
    ModelApprovalGrantLike,
)
from agent_harness.models._route_chain_state import (
    activate_approved_route,
    advance_from_approved_balance_anchor,
    deny_after_approved_balance_anchor,
    mark_approved_route_balance_ineligible,
    mark_route_budget_ineligible,
    mark_route_static_ineligible,
    wait_after_approved_balance_anchor,
)
from agent_harness.models._router_contracts import ModelRouteCandidate, ModelRouteChainPlan
from agent_harness.models.providers import (
    ModelRequest,
)
from agent_harness.models.route_chain_identity import (
    ModelRouteApprovalGrantIdentity,
    ModelRouteApprovalRequestIdentity,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.policy import PolicyCheck
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.shared_budget import (
    BudgetReservationRejected,
)

if TYPE_CHECKING:
    from agent_harness.models._settlement_contracts import SettlementStart


class _ChainApprovalMixin(ChainRuntimeBase):
    async def _activate_or_skip_approved_route(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        approved_grant: ModelApprovalGrantLike,
        request_binding_digest: str,
        usage_call_id: str,
        operation_identity_digest: str,
        actor: IdentityContext | None,
        cost_enabled: bool,
        settlement: SettlementStart,
        stream: bool,
    ) -> ModelRouteChainState:
        """激活 waiting ordinal；余额不足则保留 grant binding 并重新授权后继。"""

        anchor_ordinal = state.waiting_approval_ordinal
        if anchor_ordinal is None:
            raise ValueError("route chain is not waiting for approval")
        grant_digest = self._route_approval_grant_digest(
            approved_grant=approved_grant,
            request_binding_digest=request_binding_digest,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
        )
        activated = activate_approved_route(
            state,
            chain=chain,
            approval_grant_binding_digest=grant_digest,
            cost_enabled=cost_enabled,
        )
        try:
            return await self._persist_route_chain_state(
                context=context,
                usage_call_id=usage_call_id,
                state=activated,
                method="activate_approved_model_route",
            )
        except BudgetReservationRejected as exc:
            if exc.reason != "balance_insufficient":
                raise
        skipped = mark_approved_route_balance_ineligible(
            state,
            approval_grant_binding_digest=grant_digest,
        )
        skipped = await self._persist_route_chain_state(
            context=context,
            usage_call_id=usage_call_id,
            state=skipped,
            method="skip_approved_model_route_balance",
        )
        return await self._advance_after_approved_balance(
            request=request,
            context=context,
            chain=chain,
            state=skipped,
            anchor_ordinal=anchor_ordinal,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            actor=actor,
            cost_enabled=cost_enabled,
            settlement=settlement,
            stream=stream,
        )

    async def _advance_after_approved_balance(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        chain: ModelRouteChainPlan,
        state: ModelRouteChainState,
        anchor_ordinal: int,
        usage_call_id: str,
        operation_identity_digest: str,
        actor: IdentityContext | None,
        cost_enabled: bool,
        settlement: SettlementStart,
        stream: bool,
    ) -> ModelRouteChainState:
        """从获批 balance anchor 开始，后继逐个重新执行独立 Policy/HITL。"""

        scan_state = state
        for successor in chain.candidates[anchor_ordinal:]:
            if successor.static_ineligible_cause is not None:
                scan_state = mark_route_static_ineligible(
                    scan_state,
                    candidate_ordinal=successor.ordinal,
                )
                continue
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
                denied = deny_after_approved_balance_anchor(
                    scan_state,
                    anchor_ordinal=anchor_ordinal,
                    target_ordinal=successor.ordinal,
                )
                denied = await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=denied,
                    method="transfer_model_route_reservation",
                )
                await self._finalize_chain_terminal(
                    context=context,
                    chain=chain,
                    state=denied,
                    usage_call_id=usage_call_id,
                    settlement=settlement,
                    error_code="model.policy_denied",
                    stream=stream,
                    publish_started=True,
                )
            if policy.decision == "require_approval":
                if policy.request is None or policy.request_binding_digest is None:
                    raise RuntimeError("approval policy outcome omitted its binding")
                waiting = wait_after_approved_balance_anchor(
                    scan_state,
                    anchor_ordinal=anchor_ordinal,
                    target_ordinal=successor.ordinal,
                    approval_request_binding_digest=policy.request_binding_digest,
                )
                await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=waiting,
                    method="transfer_model_route_reservation",
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
            candidate_state = advance_from_approved_balance_anchor(
                scan_state,
                chain=chain,
                anchor_ordinal=anchor_ordinal,
                target_ordinal=successor.ordinal,
                cost_enabled=cost_enabled,
            )
            try:
                return await self._persist_route_chain_state(
                    context=context,
                    usage_call_id=usage_call_id,
                    state=candidate_state,
                    method="transfer_model_route_reservation",
                )
            except BudgetReservationRejected as exc:
                if exc.reason != "balance_insufficient":
                    raise
                scan_state = mark_route_budget_ineligible(
                    scan_state,
                    candidate_ordinal=successor.ordinal,
                )

        exhausted = advance_from_approved_balance_anchor(
            scan_state,
            chain=None,
            anchor_ordinal=anchor_ordinal,
            target_ordinal=None,
            cost_enabled=cost_enabled,
        )
        exhausted = await self._persist_route_chain_state(
            context=context,
            usage_call_id=usage_call_id,
            state=exhausted,
            method="transfer_model_route_reservation",
        )
        await self._finalize_chain_terminal(
            context=context,
            chain=chain,
            state=exhausted,
            usage_call_id=usage_call_id,
            settlement=settlement,
            error_code="model.route_chain_exhausted",
            stream=stream,
            publish_started=True,
        )
        raise AssertionError("route-chain terminal finalizer must raise")

    async def _require_chain_policy_allow(
        self,
        *,
        request: ModelRequest,
        candidate: ModelRouteCandidate,
        context: UsageEvidenceContext,
        actor: IdentityContext | None,
        chain: ModelRouteChainPlan,
        usage_call_id: str,
        operation_identity_digest: str,
    ) -> ChainPolicyOutcome:
        """返回封闭候选决策；调用方负责在 provider 前持久化对应影响。"""

        if self._policy_engine is None:
            return ChainPolicyOutcome(decision="allow")
        if actor is None:
            raise RuntimeError("model policy requires bound identity")
        policy = await self._policy_engine.evaluate(
            PolicyCheck(
                actor=actor,
                action="model.invoke",
                resource=f"agent:{context.agent_id}:model",
                context={
                    "tenant_id": context.tenant_id,
                    "agent_id": context.agent_id,
                    "run_id": context.run_id,
                    "request_id": context.request_id,
                    "trace_id": context.trace_id,
                    "candidate_ordinal": candidate.ordinal,
                    "deployment_id": candidate.deployment_id,
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "model_catalog_ref": candidate.model_catalog_ref,
                    "model_catalog_version": candidate.model_catalog_version,
                    "model_catalog_digest": candidate.model_catalog_digest,
                    "reserved_token_bound": candidate.reserved_token_bound,
                    "reserved_cost_bound": (
                        None
                        if candidate.reserved_cost_bound is None
                        else float(candidate.reserved_cost_bound)
                    ),
                    "soft_decision": candidate.route.decision.action,
                },
            )
        )
        if policy.decision == GuardrailDecisionStatus.DENY.value:
            return ChainPolicyOutcome(decision="deny")
        if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
            from agent_harness.runtime.executor import AgentApprovalRequest

            arguments_hash = hashlib.sha256(
                json.dumps(
                    request.to_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            approval_request = AgentApprovalRequest(
                action="model.invoke",
                resource=f"agent:{context.agent_id}:model",
                reason=policy.reason,
                arguments_ref=f"model-request:{arguments_hash}",
                arguments_hash=arguments_hash,
                continuation={
                    "kind": "policy_approval",
                    "route_chain_id": chain.chain_id,
                    "usage_call_id": usage_call_id,
                    "operation_identity_digest": operation_identity_digest,
                    "candidate_ordinal": candidate.ordinal,
                },
            )
            binding = ModelRouteApprovalRequestIdentity(
                schema_version="model-route-chain-approval-request-v1",
                chain_id=chain.chain_id,
                candidate_ordinal=candidate.ordinal,
                route_digest=candidate.route_digest,
                usage_call_id=usage_call_id,
                operation_identity_digest=operation_identity_digest,
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                action="model.invoke",
                resource=approval_request.resource,
                arguments_ref=approval_request.arguments_ref,
                arguments_hash=approval_request.arguments_hash,
            )
            return ChainPolicyOutcome(
                decision="require_approval",
                request=approval_request,
                request_binding_digest=binding.digest(),
            )
        return ChainPolicyOutcome(decision="allow")

    @staticmethod
    def _route_approval_grant_digest(
        *,
        approved_grant: ModelApprovalGrantLike,
        request_binding_digest: str,
        usage_call_id: str,
        operation_identity_digest: str,
    ) -> str:
        """按唯一 canonical DTO 重算 durable activation 的 grant binding。"""

        return ModelRouteApprovalGrantIdentity(
            schema_version="model-route-chain-approval-grant-v1",
            request_binding_digest=request_binding_digest,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            approval_id=str(approved_grant.approval_id),
            lease_id=str(approved_grant.lease_id),
            tenant_id=str(approved_grant.tenant_id),
            identity_id=str(approved_grant.identity_id),
            agent_id=str(approved_grant.agent_id),
            run_id=str(approved_grant.run_id),
            action=cast(Literal["model.invoke"], approved_grant.action),
            resource=str(approved_grant.resource),
            arguments_hash=str(approved_grant.arguments_hash),
        ).digest()


__all__ = ["_ChainApprovalMixin"]
