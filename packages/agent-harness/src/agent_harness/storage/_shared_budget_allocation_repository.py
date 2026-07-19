"""Delegated child allocation 与 direct settlement repository。"""

# Mixin 的跨职责 helper 由最终 repository MRO 提供；组合类仍由全仓 Pyright 校验。
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._shared_budget_repository_records import (
    _allocation_record,
    _claim_record,
    _decimal,
)
from agent_harness.storage.delegation_models import AgentDelegationModel
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    AllocationRecord,
    BudgetOperationConflict,
    BudgetReservationRejected,
    ClaimRecord,
    OperationIdentity,
    validate_actual_usage,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)

_ZERO = Decimal("0")


class _SharedBudgetAllocationMixin:
    """按同一 owner ledger lock/CAS 维护本职责内的预算事实。"""

    _session: AsyncSession

    async def allocate(self, data: AllocationBudgetClaim) -> AllocationRecord:
        """Child operation 只占用 delegation ceiling，不直接增加 parent impact。"""

        existing = await self._session.scalar(
            select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == data.tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == data.budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == data.delegation_id,
                DelegationBudgetAllocationModel.usage_call_id == data.usage_call_id,
            )
        )
        if existing is not None:
            if not self._allocation_replay_matches(existing, data.identity):
                raise BudgetOperationConflict
            return _allocation_record(existing, replayed=True)

        relation = await self._session.get(AgentDelegationModel, data.delegation_id)
        if (
            relation is None
            or relation.tenant_id != data.tenant_id
            or relation.parent_run_id != data.budget_owner_run_id
            or relation.child_run_id not in {None, data.identity.run_id}
            or relation.target_agent_id != data.identity.agent_id
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        ledger = await self._lock_ledger(data.tenant_id, data.budget_owner_run_id)
        existing = await self._session.scalar(
            select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == data.tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == data.budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == data.delegation_id,
                DelegationBudgetAllocationModel.usage_call_id == data.usage_call_id,
            )
        )
        if existing is not None:
            if not self._allocation_replay_matches(existing, data.identity):
                raise BudgetOperationConflict
            return _allocation_record(existing, replayed=True)
        self._validate_allocation_identity(data.identity, ledger)
        self._validate_static_eligibility(
            identity=data.identity,
            ledger=ledger,
            token_reservation=data.token_reservation,
            cost_reservation=data.cost_reservation,
        )
        top = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(
                BudgetOperationClaimModel.delegation_id == data.delegation_id,
                BudgetOperationClaimModel.tenant_id == data.tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == data.budget_owner_run_id,
            )
            .with_for_update()
        )
        if top is None or top.state not in {"reserved", "needs_review"}:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        token_allocated, cost_allocated = await self._allocation_impact_sums(data.delegation_id)
        cost = _decimal(data.cost_reservation)
        if token_allocated + data.token_reservation > top.reserved_tokens or (
            ledger.cost_enabled and cost_allocated + cost > _decimal(top.reserved_cost)
        ):
            raise BudgetReservationRejected(reason="balance_insufficient")
        state = "settled" if data.zero_impact else "reserved"
        side_effect_state = "result_committed" if data.zero_impact else "not_started"
        model = DelegationBudgetAllocationModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.budget_owner_run_id,
            delegation_id=data.delegation_id,
            usage_call_id=data.usage_call_id,
            run_id=data.identity.run_id,
            agent_id=data.identity.agent_id,
            usage_kind=data.identity.usage_kind,
            identity_schema_version=data.identity.identity_schema_version,
            identity_hash=data.identity.identity_hash,
            identity_json=data.identity.to_payload(),
            reserved_tokens=data.token_reservation,
            reserved_cost=data.cost_reservation,
            actual_tokens=0 if data.zero_impact else None,
            actual_cost=None,
            token_impact=0 if data.zero_impact else data.token_reservation,
            cost_impact=_ZERO if data.zero_impact else cost,
            state=state,
            side_effect_state=side_effect_state,
            result_json=data.result,
        )
        self._session.add(model)
        await self._session.flush()
        return _allocation_record(model)

    async def preflight_allocation(self, data: AllocationBudgetClaim) -> AllocationRecord | None:
        """Allocation replay/integrity 优先于 sequence state 与当前余额竞争。"""

        existing = await self._session.scalar(
            select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == data.tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == data.budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == data.delegation_id,
                DelegationBudgetAllocationModel.usage_call_id == data.usage_call_id,
            )
        )
        if existing is not None:
            if not self._allocation_replay_matches(existing, data.identity):
                raise BudgetOperationConflict
            return _allocation_record(existing, replayed=True)
        relation = await self._session.get(AgentDelegationModel, data.delegation_id)
        if (
            relation is None
            or relation.tenant_id != data.tenant_id
            or relation.parent_run_id != data.budget_owner_run_id
            or relation.child_run_id not in {None, data.identity.run_id}
            or relation.target_agent_id != data.identity.agent_id
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        ledger = await self._lock_ledger(data.tenant_id, data.budget_owner_run_id)
        self._validate_allocation_identity(data.identity, ledger)
        top = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == data.delegation_id,
                BudgetOperationClaimModel.tenant_id == data.tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == data.budget_owner_run_id,
            )
        )
        if top is None or top.state not in {"reserved", "needs_review"}:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        return None

    async def validate_operation_identity(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        identity: OperationIdentity,
    ) -> None:
        """只校验 owner/snapshot/route 完整性，hard eligibility 留到 sequence preflight 后。"""

        ledger = await self._lock_ledger(tenant_id, budget_owner_run_id)
        if identity.ownership_kind == "direct":
            self._validate_direct_identity(identity, ledger)
        else:
            self._validate_allocation_identity(identity, ledger)

    async def mark_allocation_started(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        usage_call_id: str,
    ) -> AllocationRecord:
        """在 child 副作用启动前锁定 allocation，并将首个启动状态持久化。

        该标记用于恢复路径区分“可释放预约”和“已经可能发生外部调用”的 claim，
        所以必须经同一 tenant/owner/delegation 复合键读取。
        """

        allocation = await self._require_allocation_locked(
            tenant_id, budget_owner_run_id, delegation_id, usage_call_id
        )
        if allocation.side_effect_state == "not_started":
            allocation.side_effect_state = "started"
            await self._session.flush()
        return _allocation_record(allocation)

    async def settle_allocation(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        usage_call_id: str,
        actual_tokens: int | None,
        actual_cost: Decimal | None,
        cost_status: str,
        result: dict[str, Any],
    ) -> AllocationRecord:
        """Allocation delta 只经 top-level claim 向 parent 传播，避免 child 双计。"""

        validate_actual_usage(
            actual_tokens=actual_tokens,
            actual_cost=actual_cost,
            cost_status=cost_status,
        )
        seed = await self._session.scalar(
            select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == delegation_id,
                DelegationBudgetAllocationModel.usage_call_id == usage_call_id,
            )
        )
        if seed is None:
            raise LookupError(f"budget allocation not found: {usage_call_id}")
        if seed.side_effect_state == "result_committed":
            if seed.result_json != result:
                raise BudgetOperationConflict
            return _allocation_record(seed, replayed=True)
        ledger = await self._lock_ledger(tenant_id, budget_owner_run_id, allow_needs_review=True)
        allocation = await self._require_allocation_locked(
            tenant_id, budget_owner_run_id, delegation_id, usage_call_id
        )
        if allocation.side_effect_state == "result_committed":
            if allocation.result_json != result:
                raise BudgetOperationConflict
            return _allocation_record(allocation, replayed=True)
        top = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if top is None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        token_impact = (
            cast(int, allocation.reserved_tokens) if actual_tokens is None else actual_tokens
        )
        if ledger.cost_enabled:
            cost_impact = _decimal(allocation.reserved_cost) if actual_cost is None else actual_cost
            unknown_cost = actual_cost is None
        else:
            cost_impact = _ZERO
            unknown_cost = False
        needs_review = (
            actual_tokens is None
            or unknown_cost
            or token_impact > cast(int, allocation.reserved_tokens)
            or cost_impact > _decimal(allocation.reserved_cost)
        )
        allocation.actual_tokens = actual_tokens
        allocation.actual_cost = actual_cost
        allocation.token_impact = token_impact
        allocation.cost_impact = cost_impact
        allocation.state = "needs_review" if needs_review else "settled"
        allocation.side_effect_state = "result_committed"
        allocation.result_json = result
        await self._session.flush()
        token_sum, cost_sum = await self._allocation_impact_sums(delegation_id)
        new_top_tokens = max(top.reserved_tokens, token_sum)
        new_top_cost = max(_decimal(top.reserved_cost), cost_sum) if ledger.cost_enabled else _ZERO
        ledger.token_impact += new_top_tokens - top.token_impact
        ledger.cost_impact += new_top_cost - top.cost_impact
        top.token_impact = new_top_tokens
        top.cost_impact = new_top_cost
        if (
            needs_review
            or token_sum > top.reserved_tokens
            or (ledger.cost_enabled and cost_sum > _decimal(top.reserved_cost))
        ):
            top.state = "needs_review"
            ledger.state = "needs_review"
        ledger.version += 1
        await self._session.flush()
        return _allocation_record(allocation)

    async def settle_direct(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        usage_call_id: str,
        actual_tokens: int | None,
        actual_cost: Decimal | None,
        cost_status: str,
        result: dict[str, Any],
    ) -> ClaimRecord:
        """可信 result、impact 替换与 result_committed 在调用方 UoW 中同生共死。"""

        validate_actual_usage(
            actual_tokens=actual_tokens,
            actual_cost=actual_cost,
            cost_status=cost_status,
        )
        seed = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.operation_kind == "direct",
                BudgetOperationClaimModel.usage_call_id == usage_call_id,
            )
        )
        if seed is None:
            raise LookupError(f"budget claim not found: {usage_call_id}")
        if seed.side_effect_state == "result_committed":
            if seed.result_json != result:
                raise BudgetOperationConflict
            return _claim_record(seed, replayed=True)
        ledger = await self._lock_ledger(tenant_id, budget_owner_run_id, allow_needs_review=True)
        claim = await self._require_direct_locked(tenant_id, budget_owner_run_id, usage_call_id)
        if claim.side_effect_state == "result_committed":
            if claim.result_json != result:
                raise BudgetOperationConflict
            return _claim_record(claim, replayed=True)
        unknown_token = actual_tokens is None
        token_impact = claim.reserved_tokens if unknown_token else actual_tokens
        token_over = token_impact > claim.reserved_tokens
        if ledger.cost_enabled:
            if actual_cost is None:
                unknown_cost = True
                cost_impact = _decimal(claim.reserved_cost)
            else:
                unknown_cost = False
                cost_impact = actual_cost
            cost_over = cost_impact > _decimal(claim.reserved_cost)
        else:
            unknown_cost = False
            cost_impact = _ZERO
            cost_over = False
        needs_review = unknown_token or unknown_cost or token_over or cost_over
        ledger.token_impact += token_impact - claim.token_impact
        ledger.cost_impact += cost_impact - claim.cost_impact
        ledger.version += 1
        if needs_review:
            ledger.state = "needs_review"
        claim.actual_tokens = actual_tokens
        claim.actual_cost = actual_cost
        claim.token_impact = token_impact
        claim.cost_impact = cost_impact
        claim.state = "needs_review" if needs_review else "settled"
        claim.side_effect_state = "result_committed"
        claim.result_json = result
        await self._session.flush()
        return _claim_record(claim)


__all__ = ["_SharedBudgetAllocationMixin"]
