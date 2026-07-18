"""Direct 与 top-level delegation claim/reservation/settlement repository。"""

# Mixin 的跨职责 helper 由最终 repository MRO 提供；组合类仍由全仓 Pyright 校验。
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationBudgetExceeded,
    DelegationStorageConflict,
)
from agent_harness.storage._shared_budget_repository_records import (
    _claim_record,
    _decimal,
)
from agent_harness.storage.delegation_models import AgentDelegationModel
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetReservationRejected,
    ClaimRecord,
    DirectBudgetClaim,
    validate_actual_usage,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)

_ZERO = Decimal("0")


class _SharedBudgetDirectMixin:
    """按同一 owner ledger lock/CAS 维护本职责内的预算事实。"""

    _session: AsyncSession

    async def _direct_by_key(
        self, *, tenant_id: str, budget_owner_run_id: str, usage_call_id: str
    ) -> BudgetOperationClaimModel | None:
        return await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.operation_kind == "direct",
                BudgetOperationClaimModel.usage_call_id == usage_call_id,
            )
        )

    async def claim_direct(self, data: DirectBudgetClaim) -> ClaimRecord:
        """Identity 优先判定后，以 owner version CAS 原子取得 token/cost 余额。"""

        existing = await self._direct_by_key(
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.budget_owner_run_id,
            usage_call_id=data.usage_call_id,
        )
        if existing is not None:
            if not self._direct_replay_matches(existing, data.identity):
                raise BudgetOperationConflict
            return _claim_record(existing, replayed=True)

        ledger = await self._lock_ledger(data.tenant_id, data.budget_owner_run_id)
        # PostgreSQL 的 owner row lock 可能等待另一个同 key writer 提交；锁后必须
        # 重读 stable key，才能把 unique race 收敛成 replay/conflict 而非重复扣款。
        existing = await self._direct_by_key(
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.budget_owner_run_id,
            usage_call_id=data.usage_call_id,
        )
        if existing is not None:
            if not self._direct_replay_matches(existing, data.identity):
                raise BudgetOperationConflict
            return _claim_record(existing, replayed=True)
        self._validate_direct_identity(data.identity, ledger)
        cost = _decimal(data.cost_reservation)
        self._validate_static_eligibility(
            identity=data.identity,
            ledger=ledger,
            token_reservation=data.token_reservation,
            cost_reservation=data.cost_reservation,
        )
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ParentBudgetLedgerModel)
                .where(
                    ParentBudgetLedgerModel.tenant_id == data.tenant_id,
                    ParentBudgetLedgerModel.budget_owner_run_id == data.budget_owner_run_id,
                    ParentBudgetLedgerModel.state == "active",
                    ParentBudgetLedgerModel.version == ledger.version,
                    ParentBudgetLedgerModel.token_impact + data.token_reservation
                    <= ParentBudgetLedgerModel.token_limit,
                    (ParentBudgetLedgerModel.cost_enabled.is_(False))
                    | (
                        ParentBudgetLedgerModel.cost_impact + cost
                        <= ParentBudgetLedgerModel.cost_limit
                    ),
                )
                .values(
                    token_impact=ParentBudgetLedgerModel.token_impact + data.token_reservation,
                    cost_impact=ParentBudgetLedgerModel.cost_impact + cost,
                    version=ParentBudgetLedgerModel.version + 1,
                )
            ),
        )
        if changed.rowcount != 1:
            raced = await self._direct_by_key(
                tenant_id=data.tenant_id,
                budget_owner_run_id=data.budget_owner_run_id,
                usage_call_id=data.usage_call_id,
            )
            if raced is not None:
                if not self._direct_replay_matches(raced, data.identity):
                    raise BudgetOperationConflict
                return _claim_record(raced, replayed=True)
            current = await self._session.get(
                ParentBudgetLedgerModel,
                (data.tenant_id, data.budget_owner_run_id),
                populate_existing=True,
            )
            reason = (
                "ledger_needs_review"
                if current and current.state != "active"
                else "balance_insufficient"
            )
            raise BudgetReservationRejected(reason=reason)

        identity = data.identity.to_payload()
        model = BudgetOperationClaimModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.budget_owner_run_id,
            operation_kind="direct",
            usage_call_id=data.usage_call_id,
            delegation_id=None,
            run_id=data.identity.run_id,
            agent_id=data.identity.agent_id,
            usage_kind=data.identity.usage_kind,
            identity_schema_version=data.identity.identity_schema_version,
            identity_hash=data.identity.identity_hash,
            identity_json=identity,
            request_hash=None,
            reserved_tokens=data.token_reservation,
            reserved_cost=data.cost_reservation,
            actual_tokens=0 if data.zero_impact else None,
            actual_cost=None,
            token_impact=data.token_reservation,
            cost_impact=cost,
            state="settled" if data.zero_impact else "reserved",
            side_effect_state="result_committed" if data.zero_impact else "not_started",
            result_json=data.result,
        )
        self._session.add(model)
        await self._session.flush()
        return _claim_record(model)

    async def preflight_direct(self, data: DirectBudgetClaim) -> ClaimRecord | None:
        """在 sequence preflight 前收敛 replay/conflict，并验证静态 owner/snapshot。"""

        existing = await self._direct_by_key(
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.budget_owner_run_id,
            usage_call_id=data.usage_call_id,
        )
        if existing is not None:
            if not self._direct_replay_matches(existing, data.identity):
                raise BudgetOperationConflict
            return _claim_record(existing, replayed=True)
        ledger = await self._lock_ledger(data.tenant_id, data.budget_owner_run_id)
        self._validate_direct_identity(data.identity, ledger)
        return None

    async def reserve_delegation(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        request_hash: str,
        token_reservation: int,
        cost_reservation: Decimal | None,
    ) -> ClaimRecord:
        """把既有 0015 relation 纳入同一 parent impact；调用方可在同一 UoW 新建 relation。"""

        existing = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == delegation_id
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise DelegationStorageConflict("delegation.idempotency_conflict")
            return _claim_record(existing, replayed=True)
        relation = await self._session.get(AgentDelegationModel, delegation_id)
        if (
            relation is None
            or relation.tenant_id != tenant_id
            or relation.parent_run_id != budget_owner_run_id
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        try:
            ledger = await self._lock_ledger(tenant_id, budget_owner_run_id)
        except BudgetReservationRejected as exc:
            if exc.reason == "ledger_needs_review":
                raise DelegationBudgetExceeded("delegation.budget_exceeded") from exc
            raise DelegationStorageConflict("delegation.execution_failed") from exc
        existing = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == delegation_id
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise DelegationStorageConflict("delegation.idempotency_conflict")
            return _claim_record(existing, replayed=True)
        target_limits = self._target_limits(ledger, relation.target_agent_id)
        if target_limits is None or not self._owner_allows_target(ledger, relation.target_agent_id):
            raise DelegationStorageConflict("delegation.execution_failed")
        target_token_limit, target_cost_limit = target_limits
        if ledger.cost_enabled and cost_reservation is None:
            raise DelegationBudgetExceeded("delegation.budget_exceeded")
        cost = _decimal(cost_reservation) if ledger.cost_enabled else _ZERO
        effective_target_cost_limit = (
            target_cost_limit if target_cost_limit is not None else cast(Decimal, ledger.cost_limit)
        )
        if (
            ledger.state != "active"
            or token_reservation > ledger.token_limit
            or token_reservation > target_token_limit
            or (ledger.cost_enabled and cost > cast(Decimal, ledger.cost_limit))
            or (ledger.cost_enabled and cost > effective_target_cost_limit)
            or ledger.token_impact + token_reservation > ledger.token_limit
            or (
                ledger.cost_enabled and ledger.cost_impact + cost > cast(Decimal, ledger.cost_limit)
            )
        ):
            raise DelegationBudgetExceeded("delegation.budget_exceeded")
        ledger.token_impact += token_reservation
        ledger.cost_impact += cost
        ledger.version += 1
        model = BudgetOperationClaimModel(
            id=str(uuid4()),
            tenant_id=tenant_id,
            budget_owner_run_id=budget_owner_run_id,
            operation_kind="delegation",
            usage_call_id=None,
            delegation_id=delegation_id,
            run_id=budget_owner_run_id,
            agent_id=relation.source_agent_id,
            usage_kind=None,
            identity_schema_version=None,
            identity_hash=None,
            identity_json=None,
            request_hash=request_hash,
            reserved_tokens=token_reservation,
            reserved_cost=cost_reservation,
            actual_tokens=None,
            actual_cost=None,
            token_impact=token_reservation,
            cost_impact=cost,
            state="reserved",
            side_effect_state="not_started",
            result_json=None,
        )
        self._session.add(model)
        await self._session.flush()
        return _claim_record(model)

    async def mark_direct_started(
        self, *, tenant_id: str, budget_owner_run_id: str, usage_call_id: str
    ) -> ClaimRecord:
        claim = await self._require_direct_locked(tenant_id, budget_owner_run_id, usage_call_id)
        replayed = claim.side_effect_state != "not_started"
        if claim.side_effect_state == "not_started":
            claim.side_effect_state = "started"
            await self._session.flush()
        return _claim_record(claim, replayed=replayed)

    async def mark_delegation_started(self, *, delegation_id: str) -> ClaimRecord | None:
        claim = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if claim is None:
            return None
        replayed = claim.side_effect_state != "not_started"
        if claim.side_effect_state == "not_started":
            claim.side_effect_state = "started"
            await self._session.flush()
        return _claim_record(claim, replayed=replayed)

    async def release_delegation(self, *, delegation_id: str) -> ClaimRecord | None:
        """只有 child/queue 均未开始的 0015 证明路径才能调用。"""

        claim = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if claim is None:
            return None
        if claim.state == "released":
            return _claim_record(claim, replayed=True)
        if claim.side_effect_state not in {"not_started", "started"} or claim.state != "reserved":
            raise BudgetReservationRejected(reason="ledger_needs_review")
        ledger = await self._lock_ledger(
            claim.tenant_id, claim.budget_owner_run_id, allow_needs_review=True
        )
        ledger.token_impact -= claim.token_impact
        ledger.cost_impact -= claim.cost_impact
        ledger.version += 1
        claim.token_impact = 0
        claim.cost_impact = _ZERO
        claim.state = "released"
        claim.side_effect_state = "result_committed"
        claim.result_json = {"outcome": "released", "external_side_effect": False}
        await self._session.flush()
        return _claim_record(claim)

    async def settle_delegation(
        self,
        *,
        delegation_id: str,
        actual_tokens: int | None,
        actual_cost: Decimal | None,
        cost_status: str,
        needs_review: bool,
        result: dict[str, Any],
    ) -> ClaimRecord | None:
        """可信 aggregate 只替换 top-level impact；allocation 永不直接重复计入 parent。"""

        validate_actual_usage(
            actual_tokens=actual_tokens,
            actual_cost=actual_cost,
            cost_status=cost_status,
        )
        claim = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if claim is None:
            return None
        if claim.side_effect_state == "result_committed":
            if claim.result_json != result:
                raise BudgetOperationConflict
            return _claim_record(claim, replayed=True)
        ledger = await self._lock_ledger(
            claim.tenant_id, claim.budget_owner_run_id, allow_needs_review=True
        )
        allocations = list(
            await self._session.scalars(
                select(DelegationBudgetAllocationModel)
                .where(DelegationBudgetAllocationModel.delegation_id == delegation_id)
                .with_for_update()
            )
        )
        token_sum = sum(item.token_impact for item in allocations)
        cost_sum = sum(
            (_decimal(item.cost_impact) for item in allocations),
            start=_ZERO,
        )
        allocation_uncertain = any(
            item.state in {"reserved", "needs_review"} for item in allocations
        )
        unknown_token = actual_tokens is None
        # Terminal aggregate 必须与全部 durable child allocations 逐值相等；
        # 取 max 只用于保守 impact，不能把证据矛盾伪装成 settled。
        token_aggregate_mismatch = actual_tokens is not None and actual_tokens != token_sum
        if unknown_token:
            new_token_impact = max(claim.reserved_tokens, token_sum)
        else:
            new_token_impact = max(actual_tokens, token_sum)
        if ledger.cost_enabled:
            if actual_cost is None:
                unknown_cost = True
                actual_cost_impact = _decimal(claim.reserved_cost)
            else:
                unknown_cost = False
                actual_cost_impact = actual_cost
            new_cost_impact = max(actual_cost_impact, cost_sum)
            cost_aggregate_mismatch = actual_cost is not None and actual_cost != cost_sum
        else:
            unknown_cost = False
            new_cost_impact = _ZERO
            cost_aggregate_mismatch = False
        review = (
            needs_review
            or claim.state == "needs_review"
            or allocation_uncertain
            or unknown_token
            or unknown_cost
            or token_aggregate_mismatch
            or cost_aggregate_mismatch
            or token_sum > claim.reserved_tokens
            or (ledger.cost_enabled and cost_sum > _decimal(claim.reserved_cost))
            or new_token_impact > claim.reserved_tokens
            or (ledger.cost_enabled and new_cost_impact > _decimal(claim.reserved_cost))
        )
        if review:
            # 任一 child allocation 仍 active/unknown/needs_review 时，顶层 claim
            # 必须保留原 reservation。可信 aggregate 只能抬高保守占用，不能把
            # 未决 child 降格为 settled 或提前归还 parent 余额。
            new_token_impact = max(new_token_impact, claim.reserved_tokens)
            if ledger.cost_enabled:
                new_cost_impact = max(new_cost_impact, _decimal(claim.reserved_cost))
        ledger.token_impact += new_token_impact - claim.token_impact
        ledger.cost_impact += new_cost_impact - claim.cost_impact
        ledger.version += 1
        claim.actual_tokens = actual_tokens
        claim.actual_cost = actual_cost
        claim.token_impact = new_token_impact
        claim.cost_impact = new_cost_impact
        claim.state = "needs_review" if review else "settled"
        claim.side_effect_state = "result_committed"
        claim.result_json = result
        if review:
            ledger.state = "needs_review"
        await self._session.flush()
        return _claim_record(claim)


__all__ = ["_SharedBudgetDirectMixin"]
