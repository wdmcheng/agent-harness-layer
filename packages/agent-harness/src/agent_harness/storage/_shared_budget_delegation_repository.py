"""Top-level delegation claim、replay、release 与 settlement repository。"""

# Mixin 的 ledger/catalog helper 由最终 repository MRO 提供；组合类仍由全仓 Pyright 校验。
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationBudgetExceeded,
    DelegationStorageConflict,
)
from agent_harness.storage._shared_budget_repository_records import (
    _claim_record,
    _decimal,
    _snapshot_hash,
)
from agent_harness.storage.delegation_models import AgentDelegationModel
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetReservationRejected,
    ClaimRecord,
    OperationIdentity,
    validate_actual_usage,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)

_ZERO = Decimal("0")


class _SharedBudgetDelegationMixin:
    """在 owner ledger 内维护 top-level delegation 的单次预算影响。"""

    _session: AsyncSession

    async def delegation_replay_identity_seed(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        request_hash: str,
    ) -> OperationIdentity | None:
        """不读取当前 ledger，先自校验首次持久化的顶层 delegation identity。"""

        model = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.delegation_id == delegation_id,
            )
        )
        if model is None:
            managed_ledger = await self._session.scalar(
                select(ParentBudgetLedgerModel.budget_owner_run_id).where(
                    ParentBudgetLedgerModel.tenant_id == tenant_id,
                    ParentBudgetLedgerModel.budget_owner_run_id == budget_owner_run_id,
                )
            )
            if managed_ledger is not None:
                # 只有迁移时证明为 legacy_closed 的 tree 才会同时没有 ledger
                # 与顶层 claim；托管 tree 缺 claim 是完整性损坏，不能降级放行。
                raise DelegationStorageConflict("delegation.execution_failed")
            return None
        try:
            identity = OperationIdentity.model_validate(model.identity_json)
        except (TypeError, ValueError) as exc:
            raise DelegationStorageConflict("delegation.idempotency_conflict") from exc
        if model.request_hash != request_hash or not self._delegation_replay_matches(
            model, identity
        ):
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        return identity

    async def reserve_delegation(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        request_hash: str,
        identity: OperationIdentity,
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
            if existing.request_hash != request_hash or not self._delegation_replay_matches(
                existing, identity
            ):
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
        if not self._delegation_identity_matches_snapshot(
            identity=identity,
            ledger=ledger,
            relation=relation,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
        ):
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        existing = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == delegation_id
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash or not self._delegation_replay_matches(
                existing, identity
            ):
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
            usage_kind="delegation",
            identity_schema_version=identity.identity_schema_version,
            identity_hash=identity.identity_hash,
            identity_json=identity.to_payload(),
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

    async def delegation_exact_replay_matches(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str,
        request_hash: str,
        identity: OperationIdentity,
        token_reservation: int,
        cost_reservation: Decimal | None,
    ) -> bool:
        """重放只验证首次 durable 上下文，不读取当前余额或 ledger 活动状态。"""

        model = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.delegation_id == delegation_id,
            )
        )
        relation = await self._session.get(AgentDelegationModel, delegation_id)
        return bool(
            model is not None
            and relation is not None
            and relation.tenant_id == tenant_id
            and relation.parent_run_id == budget_owner_run_id
            and relation.id == delegation_id
            and relation.source_agent_id == identity.source_agent_id
            and relation.target_agent_id == identity.target_agent_id
            and model.request_hash == request_hash
            and model.reserved_tokens == token_reservation
            and model.reserved_cost == cost_reservation
            and identity.trusted_token_bound == token_reservation
            and identity.trusted_cost_bound == cost_reservation
            and self._delegation_replay_matches(model, identity)
        )

    @staticmethod
    def _delegation_replay_matches(
        model: BudgetOperationClaimModel,
        requested: OperationIdentity,
    ) -> bool:
        """只有 0015 hash 与完整 top-level identity 都一致才允许重放。"""

        try:
            persisted = OperationIdentity.model_validate(model.identity_json)
        except (TypeError, ValueError):
            return False
        return (
            persisted.to_payload() == requested.to_payload()
            and persisted.ownership_kind == "delegation"
            and persisted.delegation_claim_id == model.delegation_id
            and persisted.run_id == model.run_id == model.budget_owner_run_id
            and persisted.agent_id == persisted.source_agent_id == model.agent_id
            and persisted.usage_kind == model.usage_kind == "delegation"
            and model.operation_kind == "delegation"
            and model.identity_schema_version == persisted.identity_schema_version
            and model.identity_hash == persisted.identity_hash
        )

    def _delegation_identity_matches_snapshot(
        self,
        *,
        identity: OperationIdentity,
        ledger: ParentBudgetLedgerModel,
        relation: AgentDelegationModel,
        token_reservation: int,
        cost_reservation: Decimal | None,
    ) -> bool:
        """核对 delegation 身份是否完全来源于冻结树快照与首次可信额度。

        这里不接受运行时 registry 或当前路由配置，避免重放、恢复时被后来
        的配置变化放宽授权边界。
        """

        raw_agents = ledger.snapshot_json.get("agents")
        agents = cast(dict[str, object], raw_agents) if isinstance(raw_agents, dict) else {}
        raw_target = agents.get(relation.target_agent_id)
        if not isinstance(raw_target, dict):
            return False
        target = cast(dict[str, object], raw_target)
        raw_routes = target.get("routes")
        if not isinstance(raw_routes, list) or not raw_routes:
            return False
        routes = cast(list[object], raw_routes)
        return (
            identity.ownership_kind == "delegation"
            and identity.identity_schema_version == "budget-delegation-v1"
            and identity.run_id == relation.parent_run_id == ledger.budget_owner_run_id
            and identity.agent_id == identity.source_agent_id == relation.source_agent_id
            and identity.target_agent_id == relation.target_agent_id
            and identity.delegation_claim_id == relation.id
            and identity.operation_slot == relation.idempotency_key
            and identity.tree_snapshot_id == ledger.snapshot_id
            and identity.agent_sub_snapshot_id == f"{ledger.snapshot_id}:{relation.target_agent_id}"
            and identity.target_route_catalog_digest == f"budget-routes-v1:{_snapshot_hash(routes)}"
            and identity.cost_enabled == ledger.cost_enabled
            and identity.trusted_token_bound == token_reservation
            and identity.trusted_cost_bound == cost_reservation
        )

    async def mark_delegation_started(self, *, delegation_id: str) -> ClaimRecord | None:
        """在 child/queue 外部副作用启动前锁定 claim，并保证重复标记可重放。"""

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

    async def _lock_delegation_after_ledger(
        self,
        *,
        delegation_id: str,
        tenant_id: str,
        budget_owner_run_id: str,
    ) -> tuple[BudgetOperationClaimModel, ParentBudgetLedgerModel]:
        """按 owner ledger、top-level claim 的全局顺序取得 mutation 锁。"""

        ledger = await self._lock_ledger(
            tenant_id,
            budget_owner_run_id,
            allow_needs_review=True,
        )
        claim = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.delegation_id == delegation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            claim is None
            or claim.tenant_id != tenant_id
            or claim.budget_owner_run_id != budget_owner_run_id
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        return claim, ledger

    async def release_delegation(self, *, delegation_id: str) -> ClaimRecord | None:
        """只有 child/queue 均未开始的 0015 证明路径才能调用。"""

        seed = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == delegation_id
            )
        )
        if seed is None:
            return None
        if seed.state == "released":
            return _claim_record(seed, replayed=True)
        claim, ledger = await self._lock_delegation_after_ledger(
            delegation_id=delegation_id,
            tenant_id=seed.tenant_id,
            budget_owner_run_id=seed.budget_owner_run_id,
        )
        if claim.state == "released":
            return _claim_record(claim, replayed=True)
        if claim.side_effect_state not in {"not_started", "started"} or claim.state != "reserved":
            raise BudgetReservationRejected(reason="ledger_needs_review")
        pending_allocation = await self._session.scalar(
            select(DelegationBudgetAllocationModel.id).where(
                DelegationBudgetAllocationModel.delegation_id == delegation_id
            )
        )
        if pending_allocation is not None:
            raise BudgetReservationRejected(reason="ledger_needs_review")
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
        seed = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == delegation_id
            )
        )
        if seed is None:
            return None
        if seed.side_effect_state == "result_committed":
            if seed.result_json != result:
                raise BudgetOperationConflict
            return _claim_record(seed, replayed=True)
        claim, ledger = await self._lock_delegation_after_ledger(
            delegation_id=delegation_id,
            tenant_id=seed.tenant_id,
            budget_owner_run_id=seed.budget_owner_run_id,
        )
        if claim.side_effect_state == "result_committed":
            if claim.result_json != result:
                raise BudgetOperationConflict
            return _claim_record(claim, replayed=True)
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


__all__ = ["_SharedBudgetDelegationMixin"]
