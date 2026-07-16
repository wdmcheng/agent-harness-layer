"""Delegation child 绑定、失败补偿与聚合结算 repository mixin。"""

from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationAggregateRecord,
    DelegationRecord,
    DelegationStorageConflict,
)
from agent_harness.storage._delegation_records import (
    aggregate_record as _aggregate_record,
)
from agent_harness.storage._delegation_records import (
    delegation_event_result as _delegation_event_result,
)
from agent_harness.storage._delegation_records import (
    delegation_group_id as _delegation_group_id,
)
from agent_harness.storage._delegation_records import (
    delegation_record as _delegation_record,
)
from agent_harness.storage._delegation_records import (
    delegation_status_from_run as _delegation_status_from_run,
)
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import (
    EventCapacityRepository,
)
from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository
from agent_harness.storage.models import AgentRunModel


class DelegationSettlementRepositoryMixin:
    _session: AsyncSession

    async def attach_child(self, *, delegation_id: str, child_run_id: str) -> DelegationRecord:
        delegation = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == delegation_id)
            .with_for_update()
        )
        child = await self._session.get(AgentRunModel, child_run_id)
        if delegation is None or child is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if delegation.child_run_id is not None and delegation.child_run_id != child_run_id:
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        already_attached = delegation.child_run_id == child_run_id
        if (
            child.tenant_id != delegation.tenant_id
            or child.parent_run_id != delegation.parent_run_id
            or child.agent_id != delegation.target_agent_id
            or child.trace_id != delegation.trace_id
            or child.idempotency_key != f"delegation:{delegation.id}"
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        if already_attached:
            return _delegation_record(delegation)
        delegation.child_run_id = child.id
        delegation.status = _delegation_status_from_run(child.status)
        await EvidenceOutboxRepository(self._session).update_group_result(
            group_id=_delegation_group_id(delegation.id),
            result=_delegation_event_result(delegation, child_run_id=child.id),
        )
        await self._session.flush()
        await self._session.refresh(delegation)
        return _delegation_record(delegation)

    async def release_pre_child_failure(self, *, delegation_id: str) -> DelegationRecord:
        """在能证明 child 未创建时，幂等释放预算与未使用的 child-event 预约。"""

        delegation = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == delegation_id)
            .with_for_update()
        )
        reservation = await self._session.scalar(
            select(DelegationBudgetReservationModel)
            .where(DelegationBudgetReservationModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if delegation is None or reservation is None or delegation.child_run_id is not None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if reservation.state == "released" and delegation.status == "failed":
            return _delegation_record(delegation)
        if reservation.state != "reserved" or delegation.status != "claimed":
            raise DelegationStorageConflict("delegation.execution_failed")
        group_id = _delegation_group_id(delegation.id)
        group = await EvidenceOutboxRepository(self._session).ordered_group(group_id=group_id)
        if len(group) != 3:
            raise DelegationStorageConflict("delegation.execution_failed")
        child_event = group[1]
        if child_event.state != "result_persisted" or child_event.reserved_event_count != 1:
            raise DelegationStorageConflict("delegation.execution_failed")
        # child 尚未创建时不会产生 child-created 事件；以取消终态结清该预约，
        # 不能伪装成已发布，也不能继续阻断 parent terminal。
        child_event.state = "cancelled"
        await EventCapacityRepository(self._session).settle(
            run_id=delegation.parent_run_id,
            reserved_event_count=child_event.reserved_event_count,
            consumed=0,
        )
        reservation.state = "released"
        delegation.status = "failed"
        delegation.error_json = {"code": "delegation.execution_failed"}
        await EvidenceOutboxRepository(self._session).update_group_result(
            group_id=group_id,
            result=_delegation_event_result(delegation),
        )
        await self._session.flush()
        await self._session.refresh(delegation)
        return _delegation_record(delegation)

    async def save_aggregation(
        self,
        *,
        delegation_id: str,
        summary: dict[str, Any],
        evidence_refs: list[str],
        needs_review: bool,
    ) -> DelegationAggregateRecord:
        delegation = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == delegation_id)
            .with_for_update()
        )
        reservation = await self._session.scalar(
            select(DelegationBudgetReservationModel)
            .where(DelegationBudgetReservationModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if delegation is None or reservation is None or delegation.child_run_id is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        child = await self._session.scalar(
            select(AgentRunModel)
            .where(AgentRunModel.id == delegation.child_run_id)
            .with_for_update()
        )
        if (
            child is None
            or child.tenant_id != delegation.tenant_id
            or child.parent_run_id != delegation.parent_run_id
            or child.agent_id != delegation.target_agent_id
            or child.trace_id != delegation.trace_id
            or child.idempotency_key != f"delegation:{delegation.id}"
            or reservation.delegation_id != delegation.id
            or reservation.tenant_id != delegation.tenant_id
            or reservation.parent_run_id != delegation.parent_run_id
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        aggregate = await self._session.scalar(
            select(DelegationAggregateModel).where(
                DelegationAggregateModel.delegation_id == delegation_id
            )
        )
        aggregate_status = "needs_review" if needs_review else "complete"
        if (
            aggregate is not None
            and aggregate.status == aggregate_status
            and aggregate.summary_json == summary
            and aggregate.evidence_refs_json == evidence_refs
        ):
            # final event 已发布后 ordered group 不再可写；相同可信聚合直接重放，
            # 避免 worker redelivery 把已完成 evidence 当成待处理状态。
            return _aggregate_record(aggregate)
        if child.status in {"failed", "cancelled"}:
            delegation.error_json = {"code": "delegation.execution_failed"}
        if aggregate is None:
            aggregate = DelegationAggregateModel(
                id=str(uuid4()),
                delegation_id=delegation.id,
                tenant_id=delegation.tenant_id,
                parent_run_id=delegation.parent_run_id,
                child_run_id=delegation.child_run_id,
                status=aggregate_status,
                summary_json=summary,
                evidence_refs_json=evidence_refs,
            )
            self._session.add(aggregate)
        else:
            aggregate.status = aggregate_status
            aggregate.summary_json = summary
            aggregate.evidence_refs_json = evidence_refs
        if needs_review:
            reservation.state = "needs_review"
            delegation.status = "needs_review"
        else:
            input_tokens = summary.get("input_tokens")
            output_tokens = summary.get("output_tokens")
            cost_usd = summary.get("cost_usd")
            if (
                not isinstance(input_tokens, int)
                or isinstance(input_tokens, bool)
                or input_tokens < 0
            ):
                raise ValueError("complete delegation summary requires input tokens")
            if (
                not isinstance(output_tokens, int)
                or isinstance(output_tokens, bool)
                or output_tokens < 0
            ):
                raise ValueError("complete delegation summary requires output tokens")
            if (
                isinstance(cost_usd, bool)
                or not isinstance(cost_usd, int | float)
                or not math.isfinite(cost_usd)
                or cost_usd < 0
            ):
                raise ValueError("complete delegation summary requires finite cost")
            reservation.settled_input_tokens = input_tokens
            reservation.settled_output_tokens = output_tokens
            reservation.settled_cost_usd = float(cost_usd)
            reservation.state = "settled"
            delegation.status = _delegation_status_from_run(child.status)
        await EvidenceOutboxRepository(self._session).update_group_result(
            group_id=_delegation_group_id(delegation.id),
            result={
                **_delegation_event_result(delegation, child_run_id=delegation.child_run_id),
                "summary": summary,
                "aggregate_status": aggregate_status,
            },
        )
        await self._session.flush()
        await self._session.refresh(aggregate)
        return _aggregate_record(aggregate)


__all__ = ["DelegationSettlementRepositoryMixin"]
