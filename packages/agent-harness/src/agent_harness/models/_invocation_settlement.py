"""Model usage claim、replay、settlement 与 event publication mixin。"""

from __future__ import annotations

import asyncio

from agent_harness.events import EventBus
from agent_harness.models._invocation_chain import ModelRouteChainExecutionMixin
from agent_harness.models._invocation_chain_settlement import ModelChainSettlementMixin
from agent_harness.models._invocation_planning import ModelInvocationPlanningMixin
from agent_harness.models._route_chain_state import (
    validate_route_chain_state_identities,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    DurableMarkStateUnknown,
    IdentityRuntime,
    ModelProviderInvocationError,
    SettlementStart,
)
from agent_harness.models.providers import ModelRequest
from agent_harness.models.router import ModelRoutePlan, ModelRouter
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
)
from agent_harness.storage.model_route_chain_state import (
    route_chain_can_start_active_candidate,
)
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationConflict,
    BudgetOperationOwnership,
    BudgetReservationRejected,
    DirectBudgetClaim,
)


class _ModelSettlementMixin(
    ModelInvocationPlanningMixin,
    ModelRouteChainExecutionMixin,
    ModelChainSettlementMixin,
):
    """承载 provider 调用前后必须保持原子一致的 usage 生命周期。

    该类仍是执行 façade 的唯一线性 owner；拆出的 planning、route-chain 与 settlement
    职责只在本层组合，避免公开执行类依赖多个直接基类的覆盖顺序。任何重放必须优先
    使用已耐久的身份与结果，不能按当前快照重新预留或再次调用 provider。
    """

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _shared_budget: IdentityRuntime | None
    _telemetry: TelemetryFacade | None
    _router: ModelRouter

    @staticmethod
    def _semantic_request(request: ModelRequest) -> dict[str, object]:
        """提取决定本次模型调用语义的字段，供 identity HMAC 比较而非直接持久化。"""

        return {
            "deployment_id": request.deployment_id,
            "provider": request.provider,
            "model": request.model,
            "capability": request.capability,
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
    ) -> SettlementStart | None:
        """先验证耐久 identity/result；当前快照只约束新执行。

        此路径在读取新路由或价格配置之前运行，使中断后重试继续绑定原操作身份；仅当
        原 claim 尚未开始外部副作用时，才返回 ``None`` 交由正常冻结快照路径恢复。
        """

        if self._shared_budget is None:
            return None
        chain_safe_to_start = False
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
            chain: ModelRouteChainPlan | None = None
            if persisted.identity_schema_version == "budget-operation-v2":
                snapshot = await uow.shared_budget.get_tree_snapshot(
                    context.tenant_id, seed.ownership.budget_owner_run_id
                )
                if snapshot is None:
                    raise BudgetReservationRejected(reason="snapshot_invalid")
                chain = self._router.plan_chain_from_snapshot(
                    request,
                    snapshot=snapshot,
                    agent_id=context.agent_id,
                )
                if chain.chain_id != persisted.route_chain_digest:
                    raise BudgetReservationRejected(reason="snapshot_invalid")
            expected = self._shared_budget.operation_identity(
                tenant_id=context.tenant_id,
                ownership_kind=seed.ownership.kind,
                run_id=context.run_id,
                agent_id=context.agent_id,
                delegation_claim_id=seed.ownership.delegation_id,
                usage_kind="model",
                operation_slot=usage_call_id,
                semantic_request=(
                    self._semantic_request(request)
                    if chain is None
                    else self._semantic_chain_request(request, chain)
                ),
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
                route_chain_digest=persisted.route_chain_digest,
                route_candidate_count=persisted.route_candidate_count,
            )
            uow.shared_budget.validate_usage_replay_identity(
                seed=seed,
                expected_identity=expected,
            )
            if request.capability == "text_stream":
                from agent_harness.storage.stream_evidence_repositories import (
                    stream_usage_event_id,
                )

                final_event_id = stream_usage_event_id(usage_call_id, "final")
            else:
                final_event_id = self._final_event_id(context.tenant_id, usage_call_id)
            usage = await uow.evidence_outbox.replay_usage(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                usage_call_id=usage_call_id,
                event_id=final_event_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            if usage is None:
                raise UsageInvocationReplayError("missing_usage_settlement")
            uow.shared_budget.validate_usage_replay_settlement(
                seed=seed,
                usage_state=usage.state,
                usage_result=usage.result_json,
            )
            if persisted.identity_schema_version == "budget-operation-v2":
                if seed.route_chain_state is None or chain is None:
                    raise BudgetOperationConflict
                try:
                    validate_route_chain_state_identities(
                        seed.route_chain_state,
                        chain=chain,
                    )
                except ValueError as exc:
                    raise BudgetOperationConflict from exc
                if usage.state == "needs_review":
                    if (
                        seed.state != "needs_review"
                        or seed.side_effect_state != "result_committed"
                        or seed.result is None
                        or set(seed.result) != {"attempt_review"}
                        or usage.error_code != "model.provider_side_effect_unknown"
                    ):
                        raise BudgetOperationConflict
                    route_state = seed.route_chain_state
                    raise ModelProviderInvocationError(
                        "model.provider_side_effect_unknown",
                        provider_called=any(
                            item.request_sent
                            or item.http_response_observed
                            or item.response_identity_observed
                            or item.usage_observed
                            or item.text_observed
                            or item.delta_observed
                            for item in route_state.attempt_lifecycle
                        ),
                        attempt_count=len(route_state.attempt_lifecycle),
                    )
            chain_safe_to_start = (
                persisted.identity_schema_version == "budget-operation-v2"
                and seed.state == "reserved"
                and usage.state == "started"
                and seed.route_chain_state is not None
                and route_chain_can_start_active_candidate(seed.route_chain_state)
            )
            if (
                persisted.identity_schema_version == "budget-operation-v2"
                and seed.side_effect_state == "started"
                and usage.state == "started"
                and not chain_safe_to_start
            ):
                await uow.shared_budget.recover_unknown_started(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=seed.ownership.budget_owner_run_id,
                )
                await uow.commit()
                route_state = seed.route_chain_state
                assert route_state is not None
                provider_called = any(
                    lifecycle.request_sent
                    or lifecycle.http_response_observed
                    or lifecycle.response_identity_observed
                    or lifecycle.usage_observed
                    or lifecycle.text_observed
                    or lifecycle.delta_observed
                    for lifecycle in route_state.attempt_lifecycle
                )
                raise ModelProviderInvocationError(
                    "model.provider_side_effect_unknown",
                    provider_called=provider_called,
                    attempt_count=len(route_state.attempt_lifecycle),
                )
        if (
            seed.state == "reserved"
            and seed.side_effect_state == "not_started"
            or chain_safe_to_start
        ):
            # 首次事务尚未开始外部副作用时，仍走正常 frozen snapshot 路径恢复。
            return None
        return SettlementStart(usage=usage, ownership=seed.ownership)

    async def _start_settlement(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        request: ModelRequest,
        plan: ModelRoutePlan,
        stream: bool = False,
    ) -> SettlementStart:
        """在同一工作单元中冻结预算身份、预留额度和 usage outbox，再允许副作用。

        所有可能改变 parent budget 的校验在创建 provider 调用前完成；如果已有同一
        claim，则返回其耐久状态而非以当前 route plan 重新解释请求。
        """

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
        """在 provider 真正开始前持久化副作用边界，使恢复代码不会误判为可安全重放。"""

        if ownership is None:
            return
        try:
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
        except asyncio.CancelledError as exc:
            # DB driver 在取消窗口可能已经提交；上层必须按 unknown 保留预约并围栏。
            raise DurableMarkStateUnknown from exc


__all__ = [
    "_ModelSettlementMixin",
    "SettlementStart",
    "ModelProviderInvocationError",
]
