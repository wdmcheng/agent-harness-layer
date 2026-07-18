"""共享 parent budget 的原子 claim、settlement 与 terminal guard。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationBudgetExceeded,
    DelegationStorageConflict,
)
from agent_harness.storage._shared_budget_allocation_repository import (
    _SharedBudgetAllocationMixin,
)
from agent_harness.storage._shared_budget_direct_repository import _SharedBudgetDirectMixin
from agent_harness.storage._shared_budget_lifecycle_repository import (
    _SharedBudgetLifecycleMixin,
)
from agent_harness.storage._shared_budget_repository_records import (
    _ledger_create_snapshot_valid,
    _ledger_record,
    _ledger_snapshot_valid,
    _snapshot_hash,
)
from agent_harness.storage.delegation_models import AgentDelegationModel
from agent_harness.storage.run_models import AgentRunModel
from agent_harness.storage.shared_budget import (
    BudgetOperationOwnership,
    BudgetReservationRejected,
    LedgerCreate,
    LedgerRecord,
)
from agent_harness.storage.shared_budget_models import (
    ParentBudgetLedgerModel,
)

_ZERO = Decimal("0")


class SharedBudgetRepository(
    _SharedBudgetDirectMixin,
    _SharedBudgetAllocationMixin,
    _SharedBudgetLifecycleMixin,
):
    """所有调用都运行在 application UoW 的同一个 AsyncSession 内。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_ledger(self, data: LedgerCreate) -> LedgerRecord:
        """Root run 创建事务中冻结 owner envelope 与 tree catalog。"""

        root = await self._session.scalar(
            select(AgentRunModel).where(
                AgentRunModel.id == data.budget_owner_run_id,
                AgentRunModel.tenant_id == data.tenant_id,
            )
        )
        if root is None or root.parent_run_id is not None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        if not _ledger_create_snapshot_valid(data, root):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        existing = await self._session.get(
            ParentBudgetLedgerModel,
            (data.tenant_id, data.budget_owner_run_id),
        )
        digest = _snapshot_hash(data.snapshot)
        if existing is not None:
            immutable = (
                existing.token_limit == data.token_limit
                and existing.cost_limit == data.cost_limit
                and existing.registry_version == data.registry_version
                and existing.config_version == data.config_version
                and existing.catalog_version == data.catalog_version
                and existing.snapshot_id == data.snapshot_id
                and existing.snapshot_hash == digest
            )
            if not immutable:
                raise BudgetReservationRejected(reason="snapshot_invalid")
            return _ledger_record(existing)
        model = ParentBudgetLedgerModel(
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.budget_owner_run_id,
            token_limit=data.token_limit,
            cost_limit=data.cost_limit,
            cost_enabled=data.cost_limit is not None,
            token_impact=0,
            cost_impact=_ZERO,
            state="active",
            version=0,
            registry_version=data.registry_version,
            config_version=data.config_version,
            catalog_version=data.catalog_version,
            snapshot_id=data.snapshot_id,
            snapshot_hash=digest,
            snapshot_json=data.snapshot,
        )
        self._session.add(model)
        await self._session.flush()
        return _ledger_record(model)

    async def get_ledger(self, tenant_id: str, budget_owner_run_id: str) -> LedgerRecord | None:
        model = await self._session.get(
            ParentBudgetLedgerModel,
            (tenant_id, budget_owner_run_id),
        )
        return None if model is None or not _ledger_snapshot_valid(model) else _ledger_record(model)

    async def get_tree_snapshot(
        self, tenant_id: str, budget_owner_run_id: str
    ) -> dict[str, Any] | None:
        """返回内部 frozen tree snapshot；不得穿过 API/DTO 边界。"""

        model = await self._session.get(
            ParentBudgetLedgerModel,
            (tenant_id, budget_owner_run_id),
        )
        return (
            None
            if model is None or not _ledger_snapshot_valid(model)
            else dict(model.snapshot_json)
        )

    async def delegation_reservation(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        source_agent_id: str,
        target_agent_id: str,
    ) -> tuple[int, Decimal | None]:
        """只从 root 创建时冻结的 target budget 生成顶层 delegation reservation。"""

        try:
            ledger = await self._lock_ledger(tenant_id, budget_owner_run_id)
        except BudgetReservationRejected as exc:
            if exc.reason == "ledger_needs_review":
                raise DelegationBudgetExceeded("delegation.budget_exceeded") from exc
            raise DelegationStorageConflict("delegation.execution_failed") from exc
        raw_owner = ledger.snapshot_json.get("owner")
        if (
            not isinstance(raw_owner, dict)
            or cast(dict[str, object], raw_owner).get("agent_id") != source_agent_id
            or not self._owner_allows_target(ledger, target_agent_id)
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        target_limits = self._target_limits(ledger, target_agent_id)
        if target_limits is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        token_limit, cost_limit = target_limits
        effective_cost_limit = (
            cost_limit if cost_limit is not None else cast(Decimal, ledger.cost_limit)
        )
        return token_limit, effective_cost_limit if ledger.cost_enabled else None

    async def resolve_operation_ownership(
        self, *, tenant_id: str, run_id: str
    ) -> BudgetOperationOwnership:
        """Root 直接归自身；child 必须由稳定 delegation key 与 parent 双重证明。"""

        run = await self._session.get(AgentRunModel, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        if run.parent_run_id is None:
            return BudgetOperationOwnership(kind="direct", budget_owner_run_id=run.id)
        prefix = "delegation:"
        delegation_id = (
            run.idempotency_key[len(prefix) :]
            if run.idempotency_key is not None
            and run.idempotency_key.startswith(prefix)
            and len(run.idempotency_key) > len(prefix)
            else None
        )
        if delegation_id is None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        relation = await self._session.get(AgentDelegationModel, delegation_id)
        if (
            relation is None
            or relation.tenant_id != tenant_id
            or relation.parent_run_id != run.parent_run_id
            or relation.child_run_id not in {None, run.id}
            or relation.target_agent_id != run.agent_id
        ):
            raise BudgetReservationRejected(reason="snapshot_invalid")
        parent = await self._session.get(AgentRunModel, run.parent_run_id)
        if parent is None or parent.tenant_id != tenant_id or parent.parent_run_id is not None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        return BudgetOperationOwnership(
            kind="allocation",
            budget_owner_run_id=parent.id,
            delegation_id=relation.id,
        )


__all__ = ["SharedBudgetRepository"]
