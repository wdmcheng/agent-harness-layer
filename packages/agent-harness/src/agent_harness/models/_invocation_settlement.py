"""Model usage claim、replay、settlement 与 event publication mixin。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from agent_harness.events import EventBus
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.router import ModelRoutePlan, ModelRouterConfig
from agent_harness.models.usage import (
    ModelUsageEvidence,
    UsageEvidenceContext,
    UsageInvocationReplayError,
)
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    UsageSettlementClaim,
)
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationOwnership,
    BudgetReservationRejected,
    DirectBudgetClaim,
    OperationIdentity,
)


@dataclass(frozen=True)
class _SettlementStart:
    usage: UsageSettlementClaim
    ownership: BudgetOperationOwnership | None
    safe_to_start: bool = False


class ModelProviderInvocationError(RuntimeError):
    """provider 原异常已封闭，调用方只能看到稳定错误码。"""

    code = "model.provider_failed"


class _IdentityRuntime(Protocol):
    def operation_identity(self, **values: Any) -> OperationIdentity: ...

    def model_router_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        base: ModelRouterConfig,
    ) -> ModelRouterConfig: ...


class _ModelSettlementMixin:
    """承载 provider 调用前后必须保持原子一致的 usage 生命周期。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _shared_budget: _IdentityRuntime | None
    _telemetry: TelemetryFacade | None

    @staticmethod
    def _final_event_id(tenant_id: str, usage_call_id: str) -> str: ...

    @staticmethod
    def _safe_decision(*parts: dict[str, object]) -> dict[str, Any]: ...

    @staticmethod
    def _durable_response(response: ModelResponse) -> dict[str, Any]: ...

    @staticmethod
    def _semantic_request(request: ModelRequest) -> dict[str, object]:
        return {
            "provider": request.provider,
            "model": request.model,
            "prompt": request.prompt,
            "max_output_tokens": request.max_output_tokens,
            "timeout_seconds": request.timeout_seconds,
        }

    async def _replay_settlement_before_current_snapshot(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        usage_call_id: str,
    ) -> _SettlementStart | None:
        """先验证 durable identity/result；当前 snapshot 只约束新执行。"""

        if self._shared_budget is None:
            return None
        async with self._storage.uow() as uow:
            seed = await uow.shared_budget.usage_replay_seed(
                tenant_id=context.tenant_id,
                usage_call_id=usage_call_id,
            )
            if seed is None:
                return None
            persisted = seed.identity
            assert persisted.provider is not None
            assert persisted.model is not None
            expected = self._shared_budget.operation_identity(
                tenant_id=context.tenant_id,
                ownership_kind=seed.ownership.kind,
                run_id=context.run_id,
                agent_id=context.agent_id,
                delegation_claim_id=seed.ownership.delegation_id,
                usage_kind="model",
                operation_slot=usage_call_id,
                semantic_request=self._semantic_request(request),
                tree_snapshot_id=persisted.tree_snapshot_id,
                agent_sub_snapshot_id=persisted.agent_sub_snapshot_id,
                provider=persisted.provider,
                model=persisted.model,
                price_source_ref=persisted.price_source_ref,
                price_source_version=persisted.price_source_version,
                cache_key_digest=persisted.cache_key_digest,
                cost_enabled=persisted.cost_enabled,
                trusted_token_bound=persisted.trusted_token_bound,
                trusted_cost_bound=persisted.trusted_cost_bound,
            )
            uow.shared_budget.validate_usage_replay_identity(
                seed=seed,
                expected_identity=expected,
            )
            usage = await uow.evidence_outbox.replay_usage(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(context.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            if usage is None:
                raise UsageInvocationReplayError("missing_usage_settlement")
            uow.shared_budget.validate_usage_replay_settlement(
                seed=seed,
                usage_state=usage.state,
                usage_result=usage.result_json,
            )
        if seed.state == "reserved" and seed.side_effect_state == "not_started":
            # 首次事务尚未开始外部副作用时，仍走正常 frozen snapshot 路径恢复。
            return None
        return _SettlementStart(usage=usage, ownership=seed.ownership)

    async def _start_settlement(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        request: ModelRequest,
        plan: ModelRoutePlan,
    ) -> _SettlementStart:
        async with self._storage.uow() as uow:
            ownership: BudgetOperationOwnership | None = None
            safe_to_start = False
            if self._shared_budget is not None:
                resolved = await uow.shared_budget.resolve_operation_ownership(
                    tenant_id=evidence.tenant_id,
                    run_id=evidence.run_id,
                )
                ledger = await uow.shared_budget.get_ledger(
                    evidence.tenant_id, resolved.budget_owner_run_id
                )
                if ledger is None:
                    raise BudgetReservationRejected(reason="snapshot_invalid")
                else:
                    budget_claim = None
                    trusted_cost = (
                        plan.trusted_cost_bound if ledger.cost_limit is not None else None
                    )
                    if ledger.cost_limit is not None and trusted_cost is None:
                        # Exact replay 已在本 UoW 前完成；对新请求，sequence 完整性
                        # 必须先于 intent_unbounded，且失败时不能写拒绝 evidence。
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
                        semantic_request=self._semantic_request(request),
                        tree_snapshot_id=ledger.snapshot_id,
                        agent_sub_snapshot_id=f"{ledger.snapshot_id}:{evidence.agent_id}",
                        provider=plan.provider,
                        model=plan.model,
                        price_source_ref=plan.decision.price_source_ref,
                        price_source_version=plan.decision.price_source_version,
                        cache_key_digest=None,
                        cost_enabled=ledger.cost_limit is not None,
                        trusted_token_bound=plan.trusted_token_bound,
                        trusted_cost_bound=trusted_cost,
                    )
                    if plan.decision.action == "policy_required":
                        # Soft review/approval 只验证 frozen hard eligibility；它不创建
                        # shared claim，也不占用等待期间可能变化的 parent 余额。
                        await uow.shared_budget.validate_operation_identity(
                            tenant_id=evidence.tenant_id,
                            budget_owner_run_id=resolved.budget_owner_run_id,
                            identity=identity,
                        )
                        await uow.event_capacity.assert_sequence_state_valid(
                            tenant_id=evidence.tenant_id,
                            run_id=evidence.run_id,
                        )
                        await uow.shared_budget.validate_static_intent(
                            tenant_id=evidence.tenant_id,
                            budget_owner_run_id=resolved.budget_owner_run_id,
                            identity=identity,
                            token_reservation=plan.trusted_token_bound,
                            cost_reservation=trusted_cost,
                        )
                    elif resolved.kind == "direct":
                        direct = DirectBudgetClaim(
                            tenant_id=evidence.tenant_id,
                            budget_owner_run_id=resolved.budget_owner_run_id,
                            usage_call_id=usage_call_id,
                            identity=identity,
                            token_reservation=plan.trusted_token_bound,
                            cost_reservation=trusted_cost,
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
                            token_reservation=plan.trusted_token_bound,
                            cost_reservation=trusted_cost,
                        )
                        budget_claim = await uow.shared_budget.preflight_allocation(allocation)
                        if budget_claim is None:
                            await uow.event_capacity.assert_sequence_state_valid(
                                tenant_id=evidence.tenant_id,
                                run_id=evidence.run_id,
                            )
                            budget_claim = await uow.shared_budget.allocate(allocation)
                    if plan.decision.action != "policy_required":
                        assert budget_claim is not None
                        ownership = resolved
                        safe_to_start = (
                            budget_claim.replayed
                            and budget_claim.state == "reserved"
                            and budget_claim.side_effect_state == "not_started"
                        )
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=evidence.tenant_id,
                run_id=evidence.run_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(evidence.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            await uow.commit()
            return _SettlementStart(
                usage=claim,
                ownership=ownership,
                safe_to_start=safe_to_start,
            )

    async def _record_budget_rejection(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        reason: str,
    ) -> None:
        """硬拒绝也原子保存 usage 结果；公开 evidence 只暴露统一错误码。"""

        evidence = evidence.model_copy(
            update={
                "decision": self._safe_decision(
                    {
                        "action": "rejected",
                        "provider_called": False,
                        "budget_rejection_reason": reason,
                    },
                )
            }
        )
        result = {"evidence": evidence.to_payload(), "outcome": "rejected"}
        async with self._storage.uow() as uow:
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=evidence.tenant_id,
                run_id=evidence.run_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(evidence.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            if claim.created:
                await uow.evidence_outbox.persist_result(
                    tenant_id=evidence.tenant_id,
                    usage_call_id=usage_call_id,
                    result=result,
                    error_code=BudgetReservationRejected.code,
                )
            await uow.commit()
        if not claim.created:
            await self._resume_existing_settlement(claim=claim, usage_call_id=usage_call_id)
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        )
        started = await lifecycle.publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started)
        await self._publish_final(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome="rejected",
            error_code=BudgetReservationRejected.code,
        )

    async def _mark_side_effect_started(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        ownership: BudgetOperationOwnership | None,
    ) -> None:
        if ownership is None:
            return
        async with self._storage.uow() as uow:
            if ownership.kind == "direct":
                await uow.shared_budget.mark_direct_started(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=ownership.budget_owner_run_id,
                    usage_call_id=usage_call_id,
                )
            else:
                assert ownership.delegation_id is not None
                await uow.shared_budget.mark_allocation_started(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=ownership.budget_owner_run_id,
                    delegation_id=ownership.delegation_id,
                    usage_call_id=usage_call_id,
                )
            await uow.commit()

    async def _resume_existing_settlement(
        self,
        *,
        claim: UsageSettlementClaim,
        usage_call_id: str,
    ) -> ModelResponse:
        """已有可信结果补投 event 后返回原 response；绝不重放 provider。"""

        if claim.state == "result_persisted" and claim.result_json is not None:
            result = claim.result_json
            await self._publish_final(
                evidence=ModelUsageEvidence.model_validate(result["evidence"]),
                usage_call_id=usage_call_id,
                outcome=str(result["outcome"]),
                error_code=claim.error_code,
            )
            return self._replayed_response(result, claim=claim)
        if claim.state == "published" and claim.result_json is not None:
            return self._replayed_response(claim.result_json, claim=claim)
        raise UsageInvocationReplayError(claim.state)

    @staticmethod
    def _replayed_response(
        result: dict[str, Any],
        *,
        claim: UsageSettlementClaim,
    ) -> ModelResponse:
        raw = result.get("response")
        if isinstance(raw, dict):
            return ModelResponse.model_validate(raw)
        if claim.error_code == ModelProviderInvocationError.code:
            raise ModelProviderInvocationError("model provider invocation failed")
        # pre-0016 result 没有可恢复业务 response 时必须 fail closed，不能把
        # usage evidence 猜成 provider 输出或再次调用 provider。
        raise UsageInvocationReplayError(claim.state)

    async def _finalize(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
        ownership: BudgetOperationOwnership | None,
        response: ModelResponse | None,
    ) -> None:
        result = {
            "evidence": evidence.to_payload(),
            "outcome": outcome,
            **({"response": self._durable_response(response)} if response is not None else {}),
        }
        async with self._storage.uow() as uow:
            await uow.evidence_outbox.persist_result(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
                result=result,
                error_code=error_code,
            )
            if ownership is not None:
                provider_called = evidence.decision.get("provider_called") is True
                input_tokens = evidence.input_tokens
                output_tokens = evidence.output_tokens
                actual_tokens = (
                    (input_tokens or 0) + (output_tokens or 0)
                    if not provider_called
                    or (input_tokens is not None and output_tokens is not None)
                    else None
                )
                actual_cost = None if evidence.cost_usd is None else Decimal(str(evidence.cost_usd))
                if ownership.kind == "direct":
                    await uow.shared_budget.settle_direct(
                        tenant_id=evidence.tenant_id,
                        budget_owner_run_id=ownership.budget_owner_run_id,
                        usage_call_id=usage_call_id,
                        actual_tokens=actual_tokens,
                        actual_cost=actual_cost,
                        cost_status=evidence.cost_status,
                        result=result,
                    )
                else:
                    assert ownership.delegation_id is not None
                    await uow.shared_budget.settle_allocation(
                        tenant_id=evidence.tenant_id,
                        budget_owner_run_id=ownership.budget_owner_run_id,
                        delegation_id=ownership.delegation_id,
                        usage_call_id=usage_call_id,
                        actual_tokens=actual_tokens,
                        actual_cost=actual_cost,
                        cost_status=evidence.cost_status,
                        result=result,
                    )
            await uow.commit()
        await self._publish_final(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome=outcome,
            error_code=error_code,
        )

    async def _publish_final(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        )
        final = await lifecycle.publish_final(outcome=outcome, error_code=error_code)
        if self._telemetry is not None:
            await self._telemetry.publish_event(final)
        async with self._storage.uow() as uow:
            item = await uow.evidence_outbox.get_usage(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
            )
            if not self._event_bus.capacity_managed:
                await uow.event_capacity.record_local_published(
                    run_id=evidence.run_id,
                    reserved_event_count=item.reserved_event_count,
                    highest_persisted_seq=final.seq,
                )
            await uow.evidence_outbox.mark_published(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
            )
            await uow.commit()


__all__ = [
    "_ModelSettlementMixin",
    "_SettlementStart",
    "ModelProviderInvocationError",
]
