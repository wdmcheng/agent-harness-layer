"""Embedding claim、cache replay、settlement 与 event publication mixin。"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from agent_harness.embeddings._invocation_replay import (
    _EmbeddingReplayMixin,
    _EmbeddingSettlement,
    _IdentityRuntime,
)
from agent_harness.embeddings.provider import (
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import EventBus
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
)


class EmbeddingProviderInvocationError(RuntimeError):
    """embedding provider 原异常已封闭，避免 input/header/response 泄露。"""

    code = "embedding.provider_failed"


class _EmbeddingSettlementMixin(_EmbeddingReplayMixin):
    """承载 cache/provider 调用前后必须保持原子一致的 usage 生命周期。"""

    _provider: EmbeddingProvider
    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _telemetry: TelemetryFacade | None
    _shared_budget: _IdentityRuntime | None

    async def _durable_started_evidence(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
    ) -> ModelUsageEvidence | None:
        """读取已持久化的 started 证据；实现方不得由当前请求重新构造它。"""

        ...

    def _evidence(
        self,
        *,
        context: UsageEvidenceContext,
        provider: str,
        model: str,
        cache_hit: bool,
        latency_ms: int,
        decision: dict[str, object],
    ) -> ModelUsageEvidence:
        """按当前调用上下文构造可脱敏持久化的 embedding 用量证据。"""

        ...

    @staticmethod
    def _final_event_id(tenant_id: str, usage_call_id: str) -> str:
        """返回幂等终态事件标识，供 outbox 与事件总线共享。"""

        ...

    async def _start_settlement(
        self,
        *,
        request: EmbeddingRequest,
        context: UsageEvidenceContext,
        usage_call_id: str,
        started: ModelUsageEvidence,
        cached: EmbeddingResponse | None,
        expect_replay: bool = False,
    ) -> _EmbeddingSettlement:
        """在 provider/cache 副作用前原子建立用量 outbox 与共享预算预约。

        cache 命中保留完整 evidence 生命周期但预约零影响；共享账本、claim 和 outbox
        必须在同一 UoW 成功后才允许调用方开始外部 provider。
        """

        trusted_tokens = len(request.input.encode("utf-8"))
        cache_digest = hashlib.sha256(
            f"{self._provider.provider}\0{self._provider.model}\0{request.input}".encode()
        ).hexdigest()
        zero_result = (
            {
                "evidence": started.to_payload(),
                "outcome": "completed",
                "response": cached.to_payload(),
            }
            if cached is not None
            else None
        )
        async with self._storage.uow() as uow:
            ownership: BudgetOperationOwnership | None = None
            safe_to_start = False
            if self._shared_budget is not None:
                # 快照、价格和 claim 身份从同一账本视图派生，不能混用当前配置。
                resolved = await uow.shared_budget.resolve_operation_ownership(
                    tenant_id=context.tenant_id, run_id=context.run_id
                )
                ledger = await uow.shared_budget.get_ledger(
                    context.tenant_id, resolved.budget_owner_run_id
                )
                if ledger is None:
                    raise BudgetReservationRejected(reason="snapshot_invalid")
                else:
                    snapshot = await uow.shared_budget.get_tree_snapshot(
                        context.tenant_id, resolved.budget_owner_run_id
                    )
                    if snapshot is None:
                        raise BudgetReservationRejected(reason="snapshot_invalid")
                    try:
                        frozen_price, frozen_price_ref, frozen_price_version = (
                            self._shared_budget.embedding_price_config(
                                snapshot=snapshot,
                                agent_id=context.agent_id,
                                provider=self._provider.provider,
                                model=self._provider.model,
                            )
                        )
                    except ValueError as exc:
                        raise BudgetReservationRejected(reason="snapshot_invalid") from exc
                    trusted_cost = (
                        None
                        if ledger.cost_limit is None
                        else (
                            Decimal(trusted_tokens) * frozen_price
                            if frozen_price is not None
                            else None
                        )
                    )
                    if ledger.cost_limit is not None and trusted_cost is None:
                        # 当前请求没有 durable claim 时，sequence 完整性高于所有
                        # hard-budget eligibility；失败不得进入拒绝 evidence 路径。
                        await uow.event_capacity.assert_sequence_state_valid(
                            tenant_id=context.tenant_id,
                            run_id=context.run_id,
                        )
                        raise BudgetReservationRejected(reason="intent_unbounded")
                    identity = self._shared_budget.operation_identity(
                        tenant_id=context.tenant_id,
                        ownership_kind=resolved.kind,
                        run_id=context.run_id,
                        agent_id=context.agent_id,
                        delegation_claim_id=resolved.delegation_id,
                        usage_kind="embedding",
                        operation_slot=usage_call_id,
                        semantic_request=self._semantic_request(request),
                        tree_snapshot_id=ledger.snapshot_id,
                        agent_sub_snapshot_id=f"{ledger.snapshot_id}:{context.agent_id}",
                        provider=self._provider.provider,
                        model=self._provider.model,
                        price_source_ref=frozen_price_ref,
                        price_source_version=frozen_price_version,
                        cache_key_digest=cache_digest,
                        cost_enabled=ledger.cost_limit is not None,
                        trusted_token_bound=trusted_tokens,
                        trusted_cost_bound=trusted_cost,
                    )
                    if resolved.kind == "direct":
                        direct = DirectBudgetClaim(
                            tenant_id=context.tenant_id,
                            budget_owner_run_id=resolved.budget_owner_run_id,
                            usage_call_id=usage_call_id,
                            identity=identity,
                            token_reservation=0 if cached is not None else trusted_tokens,
                            cost_reservation=(
                                _zero_cost(trusted_cost) if cached is not None else trusted_cost
                            ),
                            zero_impact=cached is not None,
                            result=zero_result,
                        )
                        budget_claim = await uow.shared_budget.preflight_direct(direct)
                        if budget_claim is None:
                            if expect_replay:
                                raise UsageInvocationReplayError("missing_shared_claim")
                            await uow.event_capacity.assert_sequence_state_valid(
                                tenant_id=context.tenant_id,
                                run_id=context.run_id,
                            )
                            budget_claim = await uow.shared_budget.claim_direct(direct)
                    else:
                        assert resolved.delegation_id is not None
                        allocation = AllocationBudgetClaim(
                            tenant_id=context.tenant_id,
                            budget_owner_run_id=resolved.budget_owner_run_id,
                            delegation_id=resolved.delegation_id,
                            usage_call_id=usage_call_id,
                            identity=identity,
                            token_reservation=0 if cached is not None else trusted_tokens,
                            cost_reservation=(
                                _zero_cost(trusted_cost) if cached is not None else trusted_cost
                            ),
                            zero_impact=cached is not None,
                            result=zero_result,
                        )
                        budget_claim = await uow.shared_budget.preflight_allocation(allocation)
                        if budget_claim is None:
                            if expect_replay:
                                raise UsageInvocationReplayError("missing_shared_allocation")
                            await uow.event_capacity.assert_sequence_state_valid(
                                tenant_id=context.tenant_id,
                                run_id=context.run_id,
                            )
                            budget_claim = await uow.shared_budget.allocate(allocation)
                    ownership = resolved
                    safe_to_start = (
                        budget_claim.replayed
                        and budget_claim.state == "reserved"
                        and budget_claim.side_effect_state == "not_started"
                    )
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(context.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.EMBEDDING_USAGE,
                started_evidence=started.to_payload(),
            )
            if expect_replay and claim.created:
                raise UsageInvocationReplayError("missing_usage_settlement")
            if cached is not None and claim.created:
                assert zero_result is not None
                await uow.evidence_outbox.persist_result(
                    tenant_id=context.tenant_id,
                    usage_call_id=usage_call_id,
                    result=zero_result,
                    error_code=None,
                )
                # 预检 lookup 必须保持只读；只有 shared claim、usage result 与
                # capacity 都已成功时，才在同一 UoW 内把 cache evidence 标记为 hit。
                await uow.embedding_cache.mark_hit(
                    tenant_id=context.tenant_id,
                    provider=cached.provider,
                    model=cached.model,
                    input_hash=cached.cache.input_hash,
                )
            await uow.commit()
            return _EmbeddingSettlement(
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
        """Embedding hard reject 与 model 共用稳定公开 code 和 durable usage 结果。"""

        evidence = evidence.model_copy(
            update={
                "decision": {
                    "action": "rejected",
                    "provider_called": False,
                    "budget_rejection_reason": reason,
                }
            }
        )
        result = {"evidence": evidence.to_payload(), "outcome": "rejected"}
        async with self._storage.uow() as uow:
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=evidence.tenant_id,
                run_id=evidence.run_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(evidence.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.EMBEDDING_USAGE,
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
        """在实际 provider 调用前标记共享预算副作用已开始，阻止不安全重放。"""

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
    ) -> EmbeddingResponse:
        """已有可信结果补投 event 后返回原 response；不重查 cache/provider。"""

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
    ) -> EmbeddingResponse:
        """从可信已结算结果恢复响应；缺失响应只允许映射为已知 provider 失败。"""

        raw = result.get("response")
        if isinstance(raw, dict):
            return EmbeddingResponse.model_validate(raw)
        if claim.error_code == EmbeddingProviderInvocationError.code:
            raise EmbeddingProviderInvocationError("embedding provider invocation failed")
        raise UsageInvocationReplayError(claim.state)

    async def recover_pending(self, *, run_id: str) -> int:
        """只补投已持久化 embedding 结果，不重新查询 cache/provider。"""

        async with self._storage.uow() as uow:
            pending = [
                (
                    item.state,
                    item.operation_kind,
                    item.result_json,
                    item.usage_call_id,
                    item.error_code,
                )
                for item in await uow.evidence_outbox.pending(run_id=run_id)
            ]
        recovered = 0
        for state, operation_kind, result, usage_call_id, error_code in pending:
            if state != "result_persisted" or operation_kind != "embedding_usage" or result is None:
                continue
            await self._publish_final(
                evidence=ModelUsageEvidence.model_validate(result["evidence"]),
                usage_call_id=str(usage_call_id),
                outcome=str(result["outcome"]),
                error_code=error_code,
            )
            recovered += 1
        return recovered

    async def _finalize(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
        ownership: BudgetOperationOwnership | None,
        response: EmbeddingResponse | None,
    ) -> None:
        """先在单一 UoW 中持久化结果并结算预算，再投递终态证据事件。"""

        result = {
            "evidence": evidence.to_payload(),
            "outcome": outcome,
            **({"response": response.to_payload()} if response is not None else {}),
        }
        # outbox 结果与账本结算必须原子提交；事件发布失败可由后续恢复安全补投。
        async with self._storage.uow() as uow:
            await uow.evidence_outbox.persist_result(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
                result=result,
                error_code=error_code,
            )
            if ownership is not None:
                actual_tokens = evidence.input_tokens
                if ownership.kind == "direct":
                    await uow.shared_budget.settle_direct(
                        tenant_id=evidence.tenant_id,
                        budget_owner_run_id=ownership.budget_owner_run_id,
                        usage_call_id=usage_call_id,
                        actual_tokens=actual_tokens,
                        actual_cost=None,
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
                        actual_cost=None,
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
        """发布终态 evidence，并在同一提交中更新容量账本和 outbox 发布状态。"""

        final = await UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        ).publish_final(outcome=outcome, error_code=error_code)
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


def _zero_cost(trusted_cost: Decimal | None) -> Decimal | None:
    """保留成本功能是否启用：无成本上限为 ``None``，命中缓存才归零。"""

    return None if trusted_cost is None else Decimal("0")


__all__ = [
    "_EmbeddingSettlementMixin",
    "EmbeddingProviderInvocationError",
]
