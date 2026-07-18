"""Shared ledger recovery、terminal 与 identity validation repository。"""

# Mixin 的跨职责 helper 由最终 repository MRO 提供；组合类仍由全仓 Pyright 校验。
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._shared_budget_repository_records import (
    _decimal,
    _ledger_snapshot_valid,
)
from agent_harness.storage.shared_budget import (
    BudgetReservationRejected,
    OperationIdentity,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)

_ZERO = Decimal("0")


class _SharedBudgetLifecycleMixin:
    """按同一 owner ledger lock/CAS 维护本职责内的预算事实。"""

    _session: AsyncSession

    async def recover_unknown_started(self, *, tenant_id: str, budget_owner_run_id: str) -> int:
        """Started 且无 result 的窗口只提升 needs_review，绝不释放或重放。"""

        ledger = await self._lock_ledger(tenant_id, budget_owner_run_id, allow_needs_review=True)
        claims = list(
            await self._session.scalars(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.tenant_id == tenant_id,
                    BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                    BudgetOperationClaimModel.side_effect_state == "started",
                    BudgetOperationClaimModel.state == "reserved",
                )
            )
        )
        for claim in claims:
            claim.state = "needs_review"
        allocations = list(
            await self._session.scalars(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.tenant_id == tenant_id,
                    DelegationBudgetAllocationModel.budget_owner_run_id == budget_owner_run_id,
                    DelegationBudgetAllocationModel.side_effect_state == "started",
                    DelegationBudgetAllocationModel.state == "reserved",
                )
            )
        )
        affected_delegations = {item.delegation_id for item in allocations}
        for allocation in allocations:
            allocation.state = "needs_review"
        if affected_delegations:
            top_claims = list(
                await self._session.scalars(
                    select(BudgetOperationClaimModel).where(
                        BudgetOperationClaimModel.delegation_id.in_(affected_delegations)
                    )
                )
            )
            for top in top_claims:
                top.state = "needs_review"
        if claims or allocations:
            ledger.state = "needs_review"
            ledger.version += 1
            await self._session.flush()
        return len(claims) + len(allocations)

    async def terminal_allowed(self, tenant_id: str, budget_owner_run_id: str) -> bool:
        ledger = await self._session.get(
            ParentBudgetLedgerModel,
            (tenant_id, budget_owner_run_id),
        )
        if ledger is None or ledger.state != "active":
            return False
        pending_claim = await self._session.scalar(
            select(func.count())
            .select_from(BudgetOperationClaimModel)
            .where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.state.in_(["reserved", "needs_review"]),
            )
        )
        pending_allocation = await self._session.scalar(
            select(func.count())
            .select_from(DelegationBudgetAllocationModel)
            .where(
                DelegationBudgetAllocationModel.tenant_id == tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == budget_owner_run_id,
                DelegationBudgetAllocationModel.state.in_(["reserved", "needs_review"]),
            )
        )
        return not pending_claim and not pending_allocation

    async def fence_terminal(self, tenant_id: str, budget_owner_run_id: str) -> None:
        ledger = await self._lock_ledger(tenant_id, budget_owner_run_id)
        if not await self.terminal_allowed(tenant_id, budget_owner_run_id):
            raise BudgetReservationRejected(reason="ledger_needs_review")
        ledger.state = "terminal"
        ledger.version += 1
        await self._session.flush()

    async def fence_terminal_if_managed(self, tenant_id: str, budget_owner_run_id: str) -> bool:
        """Legacy closed tree 没有 ledger；0016 writer 的 root 必须经过 shared guard。"""

        ledger = await self._session.get(
            ParentBudgetLedgerModel,
            (tenant_id, budget_owner_run_id),
        )
        if ledger is None:
            return False
        await self.fence_terminal(tenant_id, budget_owner_run_id)
        return True

    async def _lock_ledger(
        self,
        tenant_id: str,
        budget_owner_run_id: str,
        *,
        allow_needs_review: bool = False,
    ) -> ParentBudgetLedgerModel:
        query: Select[tuple[ParentBudgetLedgerModel]] = (
            select(ParentBudgetLedgerModel)
            .where(
                ParentBudgetLedgerModel.tenant_id == tenant_id,
                ParentBudgetLedgerModel.budget_owner_run_id == budget_owner_run_id,
            )
            .with_for_update()
        )
        ledger = await self._session.scalar(query)
        if ledger is None or not _ledger_snapshot_valid(ledger):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        if ledger.state == "terminal" or (
            ledger.state == "needs_review" and not allow_needs_review
        ):
            raise BudgetReservationRejected(reason="ledger_needs_review")
        return ledger

    def _validate_direct_identity(
        self, identity: OperationIdentity, ledger: ParentBudgetLedgerModel
    ) -> None:
        raw_owner = ledger.snapshot_json.get("owner")
        owner_agent_id = (
            cast(dict[str, object], raw_owner).get("agent_id")
            if isinstance(raw_owner, dict)
            else None
        )
        if (
            identity.ownership_kind != "direct"
            or identity.run_id != ledger.budget_owner_run_id
            or identity.agent_id != owner_agent_id
            or not self._identity_matches_snapshot(identity, ledger)
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")

    def _validate_allocation_identity(
        self, identity: OperationIdentity, ledger: ParentBudgetLedgerModel
    ) -> None:
        if (
            identity.ownership_kind != "allocation"
            or not self._owner_allows_target(ledger, identity.agent_id)
            or not self._identity_matches_snapshot(identity, ledger)
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")

    async def validate_static_intent(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        identity: OperationIdentity,
        token_reservation: int,
        cost_reservation: Decimal | None,
    ) -> None:
        """软策略前只校验冻结身份与静态 hard eligibility，不占用当前余额。"""

        ledger = await self._lock_ledger(tenant_id, budget_owner_run_id)
        if identity.ownership_kind == "direct":
            self._validate_direct_identity(identity, ledger)
        else:
            self._validate_allocation_identity(identity, ledger)
        self._validate_static_eligibility(
            identity=identity,
            ledger=ledger,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
        )

    def _validate_static_eligibility(
        self,
        *,
        identity: OperationIdentity,
        ledger: ParentBudgetLedgerModel,
        token_reservation: int,
        cost_reservation: Decimal | None,
    ) -> None:
        target_limits = self._target_limits(ledger, identity.agent_id)
        if target_limits is None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        target_token_limit, target_cost_limit = target_limits
        cost = _decimal(cost_reservation)
        effective_target_cost_limit = (
            target_cost_limit if target_cost_limit is not None else cast(Decimal, ledger.cost_limit)
        )
        if token_reservation > ledger.token_limit or token_reservation > target_token_limit:
            raise BudgetReservationRejected(reason="hard_limit_ineligible")
        if ledger.cost_enabled and (
            cost_reservation is None
            or cost > cast(Decimal, ledger.cost_limit)
            or cost > effective_target_cost_limit
        ):
            raise BudgetReservationRejected(reason="hard_limit_ineligible")

    @staticmethod
    def _target_limits(
        ledger: ParentBudgetLedgerModel, agent_id: str
    ) -> tuple[int, Decimal | None] | None:
        raw_agents = ledger.snapshot_json.get("agents", {})
        if not isinstance(raw_agents, dict):
            return None
        sub_snapshot = cast(dict[str, object], raw_agents).get(agent_id)
        if not isinstance(sub_snapshot, dict):
            return None
        raw_budget = cast(dict[str, object], sub_snapshot).get("target_budget")
        if not isinstance(raw_budget, dict):
            return None
        budget = cast(dict[str, object], raw_budget)
        token_limit = budget.get("max_tokens_per_run")
        raw_cost_limit = budget.get("max_cost_usd_per_run")
        if isinstance(token_limit, bool) or not isinstance(token_limit, int) or token_limit < 0:
            return None
        if raw_cost_limit is None:
            cost_limit = None
        else:
            try:
                cost_limit = Decimal(str(raw_cost_limit))
            except Exception:  # noqa: BLE001 - JSON snapshot 边界必须 fail closed
                return None
            if not cost_limit.is_finite() or cost_limit < 0:
                return None
        return token_limit, cost_limit

    @staticmethod
    def _owner_allows_target(ledger: ParentBudgetLedgerModel, agent_id: str) -> bool:
        raw_owner = ledger.snapshot_json.get("owner")
        if not isinstance(raw_owner, dict):
            return False
        raw_targets = cast(dict[str, object], raw_owner).get("delegation_targets")
        target_values = cast(list[object], raw_targets) if isinstance(raw_targets, list) else []
        return (
            isinstance(raw_targets, list)
            and all(isinstance(item, str) and item for item in target_values)
            and len(set(target_values)) == len(target_values)
            and agent_id in target_values
        )

    @staticmethod
    def _direct_replay_matches(
        model: BudgetOperationClaimModel, requested: OperationIdentity
    ) -> bool:
        """重算 durable identity，并绑定 direct detail 列后才允许 exact replay。"""

        try:
            persisted = OperationIdentity.model_validate(model.identity_json)
        except (TypeError, ValueError):
            return False
        return (
            persisted.to_payload() == requested.to_payload()
            and persisted.ownership_kind == "direct"
            and persisted.delegation_claim_id is None
            and model.operation_kind == "direct"
            and model.delegation_id is None
            and model.run_id == persisted.run_id == model.budget_owner_run_id
            and model.agent_id == persisted.agent_id
            and model.usage_kind == persisted.usage_kind
            and model.identity_schema_version == persisted.identity_schema_version
            and model.identity_hash == persisted.identity_hash
        )

    @staticmethod
    def _allocation_replay_matches(
        model: DelegationBudgetAllocationModel, requested: OperationIdentity
    ) -> bool:
        """Allocation exact replay 还必须绑定 child、delegation 与 stable key。"""

        try:
            persisted = OperationIdentity.model_validate(model.identity_json)
        except (TypeError, ValueError):
            return False
        return (
            persisted.to_payload() == requested.to_payload()
            and persisted.ownership_kind == "allocation"
            and persisted.delegation_claim_id == model.delegation_id
            and model.run_id == persisted.run_id
            and model.agent_id == persisted.agent_id
            and model.usage_kind == persisted.usage_kind
            and model.identity_schema_version == persisted.identity_schema_version
            and model.identity_hash == persisted.identity_hash
        )

    def _identity_matches_snapshot(
        self, identity: OperationIdentity, ledger: ParentBudgetLedgerModel
    ) -> bool:
        raw_agents = ledger.snapshot_json.get("agents", {})
        agents = cast(dict[str, object], raw_agents) if isinstance(raw_agents, dict) else {}
        sub_snapshot = agents.get(identity.agent_id)
        if not isinstance(sub_snapshot, dict):
            return False
        typed_snapshot = cast(dict[str, object], sub_snapshot)
        raw_routes = typed_snapshot.get("routes", [])
        routes = cast(list[object], raw_routes) if isinstance(raw_routes, list) else []
        route_matches = any(
            isinstance(route, dict)
            and cast(dict[str, object], route).get("usage_kind") == identity.usage_kind
            and cast(dict[str, object], route).get("provider") == identity.provider
            and cast(dict[str, object], route).get("model") == identity.model
            and cast(dict[str, object], route).get("price_source_ref") == identity.price_source_ref
            and cast(dict[str, object], route).get("price_source_version")
            == identity.price_source_version
            for route in routes
        )
        return (
            identity.tree_snapshot_id == ledger.snapshot_id
            and identity.agent_sub_snapshot_id == f"{ledger.snapshot_id}:{identity.agent_id}"
            and identity.cost_enabled == ledger.cost_enabled
            and self._target_limits(ledger, identity.agent_id) is not None
            and route_matches
        )

    async def _allocation_impact_sums(self, delegation_id: str) -> tuple[int, Decimal]:
        token_sum, cost_sum = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(DelegationBudgetAllocationModel.token_impact), 0),
                    func.coalesce(func.sum(DelegationBudgetAllocationModel.cost_impact), 0),
                ).where(DelegationBudgetAllocationModel.delegation_id == delegation_id)
            )
        ).one()
        return int(token_sum), Decimal(cost_sum)

    async def _require_allocation_locked(
        self,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        usage_call_id: str,
    ) -> DelegationBudgetAllocationModel:
        allocation = await self._session.scalar(
            select(DelegationBudgetAllocationModel)
            .where(
                DelegationBudgetAllocationModel.tenant_id == tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == delegation_id,
                DelegationBudgetAllocationModel.usage_call_id == usage_call_id,
            )
            .with_for_update()
        )
        if allocation is None:
            raise LookupError(f"budget allocation not found: {usage_call_id}")
        return allocation

    async def _require_direct_locked(
        self, tenant_id: str, budget_owner_run_id: str, usage_call_id: str
    ) -> BudgetOperationClaimModel:
        claim = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.operation_kind == "direct",
                BudgetOperationClaimModel.usage_call_id == usage_call_id,
            )
            .with_for_update()
        )
        if claim is None:
            raise LookupError(f"budget claim not found: {usage_call_id}")
        return claim


__all__ = ["_SharedBudgetLifecycleMixin"]
