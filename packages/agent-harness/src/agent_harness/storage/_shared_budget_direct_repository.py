"""Direct claim、preflight 与 started 状态 repository。"""

# Mixin 的跨职责 helper 由最终 repository MRO 提供；组合类仍由全仓 Pyright 校验。
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._shared_budget_repository_records import (
    _claim_record,
    _decimal,
)
from agent_harness.storage._shared_budget_route_chain_validation import (
    validate_initial_route_state,
)
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetReservationRejected,
    ClaimRecord,
    DirectBudgetClaim,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    ParentBudgetLedgerModel,
)


class _SharedBudgetDirectMixin:
    """按同一 owner ledger lock/CAS 维护本职责内的预算事实。"""

    _session: AsyncSession

    async def _direct_by_key(
        self, *, tenant_id: str, budget_owner_run_id: str, usage_call_id: str
    ) -> BudgetOperationClaimModel | None:
        """按 direct 幂等键读取既有 claim，不在此处修改 ledger 或 claim 状态。"""

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

        if data.route_chain_state is not None:
            validate_initial_route_state(data.route_chain_state)
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
            allow_zero_cost_coordination=(
                data.route_chain_state is not None
                and data.route_chain_state.current_reservation.token_bound == 0
                and data.route_chain_state.current_reservation.cost_bound is None
            ),
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
            route_chain_state_json=(
                None if data.route_chain_state is None else data.route_chain_state.to_payload()
            ),
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

    async def mark_direct_started(
        self, *, tenant_id: str, budget_owner_run_id: str, usage_call_id: str
    ) -> ClaimRecord:
        """在副作用实际开始前持久化 started；重复调用仅返回 replay 标记。"""

        claim = await self._require_direct_locked(tenant_id, budget_owner_run_id, usage_call_id)
        replayed = claim.side_effect_state != "not_started"
        if claim.side_effect_state == "not_started":
            claim.side_effect_state = "started"
            await self._session.flush()
        return _claim_record(claim, replayed=replayed)


__all__ = ["_SharedBudgetDirectMixin"]
