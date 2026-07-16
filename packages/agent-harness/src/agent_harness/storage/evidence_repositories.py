"""Evidence outbox 与 event capacity 的 typed UoW repository。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.event_capacity_repositories import (
    EVIDENCE_OPERATION_REGISTRY_VERSION,
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EventCapacityRepository,
    EventCapacitySnapshot,
    EvidenceOperationKind,
    OperationReservationSpec,
    operation_event_capacity,
)
from agent_harness.storage.models import (
    RunEvidenceOutboxModel,
)
from agent_harness.storage.ordered_evidence_repositories import (
    OrderedEvidenceRepositoryMixin,
)
from agent_harness.storage.usage_evidence_repositories import (
    UsageEvidenceRepositoryMixin,
)
from agent_harness.storage.usage_evidence_repositories import (
    UsageSettlementClaim as UsageSettlementClaim,
)

# 保持拆分前公开 dataclass 的模块身份，避免持久化引用漂移到私有职责模块。
UsageSettlementClaim.__module__ = __name__


class EvidenceOutboxRepository(UsageEvidenceRepositoryMixin, OrderedEvidenceRepositoryMixin):
    """usage settlement 的稳定 event-id 与 crash recovery 状态。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_event_id(self, *, event_id: str) -> RunEvidenceOutboxModel | None:
        return await self._session.scalar(
            select(RunEvidenceOutboxModel).where(RunEvidenceOutboxModel.event_id == event_id)
        )

    async def ensure_event_publishable(self, *, event_id: str) -> None:
        """锁定 ordered item，并拒绝越过尚未完成的前序 evidence。"""

        model = await self._session.scalar(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.event_id == event_id)
            .with_for_update()
        )
        if model is None or model.state not in {"result_persisted", "published"}:
            raise LookupError("evidence outbox event not publishable")
        if model.group_id is None or model.sequence_in_group is None:
            return
        predecessors = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.group_id == model.group_id,
                    RunEvidenceOutboxModel.sequence_in_group < model.sequence_in_group,
                )
                .order_by(RunEvidenceOutboxModel.sequence_in_group)
                .with_for_update()
            )
        )
        if any(item.state not in {"published", "cancelled"} for item in predecessors):
            raise LookupError("ordered evidence predecessor is not settled")

    async def mark_event_published(self, *, event_id: str) -> None:
        """逐项完成非 terminal ordered group，允许其余预约继续阻断 terminal。"""

        await self.ensure_event_publishable(event_id=event_id)
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.event_id == event_id,
                    RunEvidenceOutboxModel.state.in_(("result_persisted", "published")),
                )
                .values(state="published")
            ),
        )
        if changed.rowcount != 1:
            raise LookupError("evidence outbox event not found")

    async def pending(self, *, run_id: str) -> list[RunEvidenceOutboxModel]:
        """返回仍需发布或人工处置的记录；已发布和明确取消均为终态。"""

        result = await self._session.scalars(
            select(RunEvidenceOutboxModel).where(
                RunEvidenceOutboxModel.run_id == run_id,
                RunEvidenceOutboxModel.state.not_in(("published", "cancelled")),
            )
        )
        return list(result.all())

    async def has_pending_operation(
        self,
        *,
        run_id: str,
        operation_kind: EvidenceOperationKind,
    ) -> bool:
        """检查指定受信 operation 是否仍有未发布 evidence，不暴露业务 payload。"""

        event_id = await self._session.scalar(
            select(RunEvidenceOutboxModel.event_id)
            .where(
                RunEvidenceOutboxModel.run_id == run_id,
                RunEvidenceOutboxModel.operation_kind == operation_kind.value,
                RunEvidenceOutboxModel.state.not_in(("published", "cancelled")),
            )
            .limit(1)
        )
        return event_id is not None

    async def list_for_run(self, *, run_id: str) -> list[RunEvidenceOutboxModel]:
        """返回 run 的完整 settlement/outbox 历史，供恢复诊断与 service 证据核对。"""

        result = await self._session.scalars(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.run_id == run_id)
            .order_by(
                RunEvidenceOutboxModel.created_at.asc(),
                RunEvidenceOutboxModel.sequence_in_group.asc().nulls_first(),
                RunEvidenceOutboxModel.id.asc(),
            )
        )
        return list(result.all())


__all__ = [
    "EVIDENCE_OPERATION_REGISTRY_VERSION",
    "EventCapacityExceeded",
    "EventCapacityRepository",
    "EventCapacitySnapshot",
    "EvidenceOperationKind",
    "EvidenceOutboxRepository",
    "MAX_EVENT_SEQ",
    "OperationReservationSpec",
    "UsageSettlementClaim",
    "operation_event_capacity",
]
