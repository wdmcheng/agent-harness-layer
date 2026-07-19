"""Delegation parent/child、恢复候选与 usage evidence 查询 repository mixin。"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationAggregateRecord,
    DelegationBudgetReservationRecord,
    DelegationRecord,
    DelegationRecoveryCandidate,
    DelegationStorageConflict,
    DelegationSummaryProjectionRecord,
    DelegationUsageEvidenceRecord,
)
from agent_harness.storage._delegation_records import (
    aggregate_record as _aggregate_record,
)
from agent_harness.storage._delegation_records import (
    child_run_record as _child_run_record,
)
from agent_harness.storage._delegation_records import (
    delegation_group_id as _delegation_group_id,
)
from agent_harness.storage._delegation_records import (
    delegation_record as _delegation_record,
)
from agent_harness.storage._delegation_records import (
    reservation_record as _reservation_record,
)
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import (
    EvidenceOperationKind,
)
from agent_harness.storage.models import AgentRunModel, RunEvidenceOutboxModel


class DelegationReadRepositoryMixin:
    """提供 delegation 聚合、恢复与 usage 读取，隔离 ORM 实例生命周期。

    所有方法只返回 DTO 或受控投影，因此 service 可以在 UoW 关闭后继续进行
    evidence 发布与恢复判断，而不会把延迟加载的 ORM 状态带出事务边界。
    """

    _session: AsyncSession

    async def list_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationRecord]:
        """按创建顺序读取某个 parent 的全部 delegation 关系。"""

        models = list(
            await self._session.scalars(
                select(AgentDelegationModel)
                .where(
                    AgentDelegationModel.tenant_id == tenant_id,
                    AgentDelegationModel.parent_run_id == parent_run_id,
                )
                .order_by(AgentDelegationModel.created_at, AgentDelegationModel.id)
            )
        )
        return [_delegation_record(model) for model in models]

    async def list_summary_projection_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationSummaryProjectionRecord]:
        """固定一次联表读取 RUN-002 relation truth，避免逐 child 往返与口径漂移。"""

        rows = list(
            (
                await self._session.execute(
                    select(
                        AgentDelegationModel,
                        AgentRunModel,
                        DelegationBudgetReservationModel,
                        DelegationAggregateModel,
                    )
                    .outerjoin(
                        AgentRunModel,
                        AgentRunModel.id == AgentDelegationModel.child_run_id,
                    )
                    .outerjoin(
                        DelegationBudgetReservationModel,
                        DelegationBudgetReservationModel.delegation_id == AgentDelegationModel.id,
                    )
                    .outerjoin(
                        DelegationAggregateModel,
                        DelegationAggregateModel.delegation_id == AgentDelegationModel.id,
                    )
                    .where(
                        AgentDelegationModel.tenant_id == tenant_id,
                        AgentDelegationModel.parent_run_id == parent_run_id,
                    )
                    .order_by(AgentDelegationModel.created_at, AgentDelegationModel.id)
                )
            ).all()
        )
        return [
            DelegationSummaryProjectionRecord(
                delegation=_delegation_record(delegation),
                child=None if child is None else _child_run_record(child),
                reservation=(None if reservation is None else _reservation_record(reservation)),
                aggregate=None if aggregate is None else _aggregate_record(aggregate),
            )
            for delegation, child, reservation, aggregate in rows
        ]

    async def list_recovery_candidates_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationRecoveryCandidate]:
        """固定两次查询找出 claim/child/final 可重放项，不按 child 做 N+1。"""

        pending_rows = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.tenant_id == tenant_id,
                    RunEvidenceOutboxModel.run_id == parent_run_id,
                    RunEvidenceOutboxModel.operation_kind == EvidenceOperationKind.DELEGATION.value,
                    RunEvidenceOutboxModel.state == "result_persisted",
                )
                .order_by(
                    RunEvidenceOutboxModel.created_at,
                    RunEvidenceOutboxModel.sequence_in_group,
                    RunEvidenceOutboxModel.id,
                )
            )
        )
        phases_by_delegation: dict[str, list[str]] = {}
        for row in pending_rows:
            result = row.result_json
            if not isinstance(result, Mapping):
                raise DelegationStorageConflict("delegation.execution_failed")
            delegation_id = result.get("delegation_id")
            if (
                not isinstance(delegation_id, str)
                or result.get("parent_run_id") != parent_run_id
                or row.group_id != _delegation_group_id(delegation_id)
            ):
                raise DelegationStorageConflict("delegation.execution_failed")
            phase_prefix = f"delegation:{delegation_id}:"
            if not row.event_id.startswith(phase_prefix):
                raise DelegationStorageConflict("delegation.execution_failed")
            phase = row.event_id.removeprefix(phase_prefix)
            if phase not in {"claimed", "child", "final"}:
                raise DelegationStorageConflict("delegation.execution_failed")
            phases_by_delegation.setdefault(delegation_id, []).append(phase)
        if not phases_by_delegation:
            return []

        models = list(
            await self._session.scalars(
                select(AgentDelegationModel)
                .where(
                    AgentDelegationModel.tenant_id == tenant_id,
                    AgentDelegationModel.parent_run_id == parent_run_id,
                    AgentDelegationModel.id.in_(phases_by_delegation),
                )
                .order_by(AgentDelegationModel.created_at, AgentDelegationModel.id)
            )
        )
        if {model.id for model in models} != set(phases_by_delegation):
            raise DelegationStorageConflict("delegation.execution_failed")
        candidates: list[DelegationRecoveryCandidate] = []
        for model in models:
            phases = phases_by_delegation[model.id]
            if (
                model.child_run_id is None
                or "claimed" in phases
                or "child" in phases
                or ("final" in phases and model.status in {"completed", "failed"})
            ):
                candidates.append(
                    DelegationRecoveryCandidate(
                        delegation=_delegation_record(model),
                        pending_phases=phases,
                    )
                )
        return candidates

    async def get(self, delegation_id: str) -> DelegationRecord | None:
        """按主键读取 delegation；不存在时返回 ``None`` 而非构造占位记录。"""

        model = await self._session.get(AgentDelegationModel, delegation_id)
        return None if model is None else _delegation_record(model)

    async def get_by_child(self, child_run_id: str) -> DelegationRecord | None:
        """按 child run 反查唯一 parent delegation，用于 child 收口和恢复。"""

        model = await self._session.scalar(
            select(AgentDelegationModel).where(AgentDelegationModel.child_run_id == child_run_id)
        )
        return None if model is None else _delegation_record(model)

    async def get_reservation(
        self,
        delegation_id: str,
    ) -> DelegationBudgetReservationRecord:
        """读取 delegation 必有的预算预约，缺失即视为 durable 状态损坏。"""

        model = await self._session.scalar(
            select(DelegationBudgetReservationModel).where(
                DelegationBudgetReservationModel.delegation_id == delegation_id
            )
        )
        if model is None:
            raise LookupError("delegation reservation not found")
        return _reservation_record(model)

    async def list_aggregates_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationAggregateRecord]:
        """按创建顺序读取 parent 下已持久化的 child 聚合结果。"""

        models = list(
            await self._session.scalars(
                select(DelegationAggregateModel)
                .where(
                    DelegationAggregateModel.tenant_id == tenant_id,
                    DelegationAggregateModel.parent_run_id == parent_run_id,
                )
                .order_by(DelegationAggregateModel.created_at, DelegationAggregateModel.id)
            )
        )
        return [_aggregate_record(model) for model in models]

    async def usage_evidence_for_child(
        self,
        child_run_id: str,
    ) -> list[DelegationUsageEvidenceRecord]:
        """读取单个 child 的 usage outbox 投影，复用批量查询实现避免口径分叉。"""

        grouped = await self.usage_evidence_for_children(child_run_ids=[child_run_id])
        return grouped.get(child_run_id, [])

    async def usage_evidence_for_children(
        self,
        *,
        child_run_ids: list[str],
    ) -> dict[str, list[DelegationUsageEvidenceRecord]]:
        """批量读取 RUN-002 对账所需 usage outbox，避免逐 child 查询。"""

        if not child_run_ids:
            return {}
        models = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.run_id.in_(child_run_ids),
                    RunEvidenceOutboxModel.operation_kind.in_(
                        (
                            EvidenceOperationKind.MODEL_USAGE.value,
                            EvidenceOperationKind.EMBEDDING_USAGE.value,
                        )
                    ),
                )
                .order_by(
                    RunEvidenceOutboxModel.run_id,
                    RunEvidenceOutboxModel.created_at,
                    RunEvidenceOutboxModel.id,
                )
            )
        )
        grouped: dict[str, list[DelegationUsageEvidenceRecord]] = {}
        for model in models:
            grouped.setdefault(model.run_id, []).append(
                DelegationUsageEvidenceRecord(
                    event_id=model.event_id,
                    operation_kind=model.operation_kind,
                    state=model.state,
                    reserved_event_count=model.reserved_event_count,
                    result=model.result_json,
                )
            )
        return grouped


__all__ = ["DelegationReadRepositoryMixin"]
