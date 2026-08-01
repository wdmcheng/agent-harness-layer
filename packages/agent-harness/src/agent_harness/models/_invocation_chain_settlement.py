"""从 _invocation_settlement.py 拆出的私有职责模块；公共 façade 与顺序语义保持不变。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from agent_harness.events import EventBus
from agent_harness.models._route_chain_state import (
    initial_denied_route_chain_state,
    initial_exhausted_route_chain_state,
    initial_scanned_route_chain_state,
    initial_waiting_route_chain_state,
    validate_route_chain_state_identities,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    IdentityRuntime,
    SettlementStart,
)
from agent_harness.models._settlement_validation import SettlementValidationMixin
from agent_harness.models.providers import ModelRequest
from agent_harness.models.router import ModelRouter
from agent_harness.models.usage import (
    ModelUsageEvidence,
)
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)
from agent_harness.storage.model_route_chain_state import (
    route_chain_can_start_active_candidate,
)
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationConflict,
    BudgetReservationRejected,
    DirectBudgetClaim,
)


class ModelChainSettlementMixin(SettlementValidationMixin):
    """承载从兼容入口拆出的单一私有职责。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _shared_budget: IdentityRuntime | None
    _telemetry: TelemetryFacade | None
    _router: ModelRouter

    @staticmethod
    def _semantic_chain_request(
        request: ModelRequest, chain: ModelRouteChainPlan
    ) -> dict[str, object]:
        """把请求内容、原始 route refs 与完整冻结 chain identity 一并绑定到 v2 HMAC。"""

        return {
            "deployment_id": request.deployment_id,
            "provider": request.provider,
            "model": request.model,
            "capability": request.capability,
            "prompt": request.prompt,
            "max_output_tokens": request.max_output_tokens,
            "timeout_seconds": request.timeout_seconds,
            "route_refs": (
                None
                if request.route_refs is None
                else [item.model_dump(mode="json") for item in request.route_refs]
            ),
            "route_chain": chain.model_dump(mode="json"),
        }

    async def _start_chain_settlement(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        request: ModelRequest,
        chain: ModelRouteChainPlan,
        operation_identity_digest: str,
        waiting_approval_ordinal: int | None = None,
        approval_request_binding_digest: str | None = None,
        denied_ordinal: int | None = None,
        initial_active_ordinal: int = 1,
        initial_skips: dict[int, Literal["static_ineligible", "soft_budget", "balance"]]
        | None = None,
        initial_exhausted: bool = False,
        stream: bool = False,
    ) -> SettlementStart:
        """以 v2 identity、首候选 reservation 与完整 state 原子建立一笔 usage。"""

        if self._shared_budget is None:
            # 无 shared-budget 的纯本地运行仍需要 usage outbox，但没有耐久 chain
            # 推进来源，故受控 route chain 不允许静默降级为进程内状态。
            raise BudgetReservationRejected(reason="snapshot_invalid")
        first = chain.candidates[0]
        async with self._storage.uow() as uow:
            resolved = await uow.shared_budget.resolve_operation_ownership(
                tenant_id=evidence.tenant_id,
                run_id=evidence.run_id,
            )
            ledger = await uow.shared_budget.get_ledger(
                evidence.tenant_id, resolved.budget_owner_run_id
            )
            if ledger is None:
                raise BudgetReservationRejected(reason="snapshot_invalid")
            trusted_cost = first.route.trusted_cost_bound if ledger.cost_limit is not None else None
            if ledger.cost_limit is not None and trusted_cost is None:
                await uow.event_capacity.assert_sequence_state_valid(
                    tenant_id=evidence.tenant_id,
                    run_id=evidence.run_id,
                )
                raise BudgetReservationRejected(reason="intent_unbounded")
            identity = self._shared_budget.operation_identity(
                tenant_id=evidence.tenant_id,
                ownership_kind=resolved.kind,
                run_id=evidence.run_id,
                agent_id=evidence.agent_id,
                delegation_claim_id=resolved.delegation_id,
                usage_kind="model",
                operation_slot=usage_call_id,
                semantic_request=self._semantic_chain_request(request, chain),
                tree_snapshot_id=ledger.snapshot_id,
                agent_sub_snapshot_id=f"{ledger.snapshot_id}:{evidence.agent_id}",
                provider=first.provider,
                model=first.model,
                price_source_ref=first.route.price_source_ref,
                price_source_version=first.route.price_source_version,
                cache_key_digest=None,
                cost_enabled=ledger.cost_limit is not None,
                trusted_token_bound=first.reserved_token_bound,
                trusted_cost_bound=trusted_cost,
                route_chain_digest=chain.chain_id,
                route_candidate_count=chain.candidate_count,
            )
            skips = initial_skips or {}
            if (
                sum(value is not None for value in (waiting_approval_ordinal, denied_ordinal))
                + int(initial_exhausted)
                > 1
            ):
                raise ValueError("route cannot be waiting and denied simultaneously")
            if initial_exhausted:
                state = initial_exhausted_route_chain_state(
                    chain=chain,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                    skipped=skips,
                )
            elif denied_ordinal is not None:
                if approval_request_binding_digest is not None:
                    raise ValueError("denied route cannot carry an approval binding")
                state = initial_denied_route_chain_state(
                    chain=chain,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                    candidate_ordinal=denied_ordinal,
                    skipped=skips,
                )
            elif waiting_approval_ordinal is None:
                if approval_request_binding_digest is not None:
                    raise ValueError("approval binding requires a waiting ordinal")
                state = initial_scanned_route_chain_state(
                    chain=chain,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                    cost_enabled=ledger.cost_limit is not None,
                    active_ordinal=initial_active_ordinal,
                    skipped=skips,
                )
            else:
                if approval_request_binding_digest is None:
                    raise ValueError("waiting route requires an approval binding")
                state = initial_waiting_route_chain_state(
                    chain=chain,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                    candidate_ordinal=waiting_approval_ordinal,
                    approval_request_binding_digest=approval_request_binding_digest,
                    skipped=skips,
                )
            state_cost_reservation = (
                None
                if state.current_reservation.cost_bound is None
                else Decimal(str(state.current_reservation.cost_bound))
            )
            if resolved.kind == "direct":
                direct = DirectBudgetClaim(
                    tenant_id=evidence.tenant_id,
                    budget_owner_run_id=resolved.budget_owner_run_id,
                    usage_call_id=usage_call_id,
                    identity=identity,
                    token_reservation=state.current_reservation.token_bound,
                    cost_reservation=state_cost_reservation,
                    route_chain_state=state,
                )
                budget_claim = await uow.shared_budget.preflight_direct(direct)
                if budget_claim is None:
                    await uow.event_capacity.assert_sequence_state_valid(
                        tenant_id=evidence.tenant_id,
                        run_id=evidence.run_id,
                    )
                    budget_claim = await uow.shared_budget.claim_direct(direct)
            else:
                assert resolved.delegation_id is not None
                allocation = AllocationBudgetClaim(
                    tenant_id=evidence.tenant_id,
                    budget_owner_run_id=resolved.budget_owner_run_id,
                    delegation_id=resolved.delegation_id,
                    usage_call_id=usage_call_id,
                    identity=identity,
                    token_reservation=state.current_reservation.token_bound,
                    cost_reservation=state_cost_reservation,
                    route_chain_state=state,
                )
                budget_claim = await uow.shared_budget.preflight_allocation(allocation)
                if budget_claim is None:
                    await uow.event_capacity.assert_sequence_state_valid(
                        tenant_id=evidence.tenant_id,
                        run_id=evidence.run_id,
                    )
                    budget_claim = await uow.shared_budget.allocate(allocation)
            if budget_claim.replayed:
                if budget_claim.route_chain_state is None:
                    raise BudgetOperationConflict
                state = budget_claim.route_chain_state
                try:
                    validate_route_chain_state_identities(state, chain=chain)
                except ValueError as exc:
                    raise BudgetOperationConflict from exc
                existing_usage = await uow.evidence_outbox.get_usage(
                    tenant_id=evidence.tenant_id,
                    usage_call_id=usage_call_id,
                )
                persisted_started = (
                    existing_usage.result_json.get("started")
                    if isinstance(existing_usage.result_json, dict)
                    else None
                )
                if not isinstance(persisted_started, dict):
                    raise BudgetOperationConflict
                # started evidence 是整笔 usage 的不可变身份锚点；恢复后执行 B
                # 不能把它改写成 B 的当前 route state，否则会伪造第二笔调用。
                evidence = ModelUsageEvidence.model_validate(persisted_started)
            else:
                evidence_candidate = chain.candidates[state.evidence_route_ordinal - 1]
                decision = self._safe_decision(
                    evidence.decision,
                    {"route": self._route_evidence(evidence_candidate.route)},
                )
                # chain DTO 已在构造边界拒绝 secret value/URL/unknown 字段；这里必须
                # 保留数字与 digest exact shape，不能再按通用敏感键名把 token_bound
                # 改写为字符串，否则耐久重放将无法复算 state。
                decision["route_chain"] = {
                    "schema_version": "model-route-chain-evidence-v1",
                    "identity": chain.model_dump(mode="json"),
                    "state": state.to_payload(),
                }
                evidence = evidence.model_copy(
                    update={
                        "provider": evidence_candidate.provider,
                        "model": evidence_candidate.model,
                        "decision": decision,
                    }
                )
            if stream:
                from agent_harness.storage.stream_evidence_repositories import (
                    stream_usage_event_id,
                )

                final_event_id = stream_usage_event_id(usage_call_id, "final")
            else:
                final_event_id = self._final_event_id(evidence.tenant_id, usage_call_id)
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=evidence.tenant_id,
                run_id=evidence.run_id,
                usage_call_id=usage_call_id,
                event_id=final_event_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            if stream:
                await uow.evidence_outbox.claim_stream(
                    tenant_id=evidence.tenant_id,
                    run_id=evidence.run_id,
                    usage_call_id=usage_call_id,
                )
            await uow.commit()
        return SettlementStart(
            usage=claim,
            ownership=resolved,
            started_evidence=evidence,
            safe_to_start=(
                budget_claim.replayed
                and budget_claim.state == "reserved"
                and budget_claim.route_chain_state is not None
                and route_chain_can_start_active_candidate(budget_claim.route_chain_state)
            ),
        )
