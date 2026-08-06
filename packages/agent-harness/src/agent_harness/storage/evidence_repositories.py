"""Evidence outbox 与 event capacity 的 typed UoW repository。"""

from __future__ import annotations

from collections.abc import Collection, Mapping
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
    EventSequenceStateInvalid,
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
from agent_harness.storage.stream_evidence_repositories import (
    StreamEvidenceRepositoryMixin,
    require_complete_settled_predecessors,
)
from agent_harness.storage.usage_evidence_repositories import (
    UsageEvidenceRepositoryMixin,
)
from agent_harness.storage.usage_evidence_repositories import (
    UsageSettlementClaim as UsageSettlementClaim,
)

# 保持拆分前公开 dataclass 的模块身份，避免持久化引用漂移到私有职责模块。
UsageSettlementClaim.__module__ = __name__


class EvidenceOutboxRepository(
    StreamEvidenceRepositoryMixin,
    UsageEvidenceRepositoryMixin,
    OrderedEvidenceRepositoryMixin,
):
    """usage settlement 的稳定 event-id 与 crash recovery 状态。"""

    def __init__(self, session: AsyncSession) -> None:
        """复用当前 UoW 的会话，保证 outbox 状态与业务写入原子提交。"""

        self._session = session

    async def get_by_event_id(self, *, event_id: str) -> RunEvidenceOutboxModel | None:
        """按全局事件标识读取 outbox 行，供幂等恢复路径核对既有状态。"""

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
        predecessor_rows = (
            await self._session.execute(
                select(
                    RunEvidenceOutboxModel.sequence_in_group,
                    RunEvidenceOutboxModel.state,
                )
                .where(
                    RunEvidenceOutboxModel.group_id == model.group_id,
                    RunEvidenceOutboxModel.sequence_in_group < model.sequence_in_group,
                )
                .order_by(RunEvidenceOutboxModel.sequence_in_group)
                .with_for_update()
            )
        ).all()
        require_complete_settled_predecessors(
            current_sequence=model.sequence_in_group,
            predecessors=[(sequence, str(state)) for sequence, state in predecessor_rows],
        )

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

    async def blocks_model_loop_terminal(
        self,
        *,
        run_id: str,
        in_flight_approval_ids: Collection[str],
    ) -> bool:
        """锁定未决outbox，并只豁免ApprovalService的受控循环依赖组。

        approve 的公开状态和两项 ordered evidence 必须等 run terminal 后才能最终
        发布，而 run terminal 又依赖内部模型循环先返回成功。这里仅接受 exact
        ``approval_resolution -> run_terminal`` 两项组均已 ``result_persisted``、
        decision=approved 且 run_status=pending 的循环中间态；其他 pending/unknown
        evidence 继续阻断 loop terminal。
        """

        pending = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.run_id == run_id,
                    RunEvidenceOutboxModel.state.not_in(("published", "cancelled")),
                )
                .order_by(
                    RunEvidenceOutboxModel.group_id.asc().nulls_first(),
                    RunEvidenceOutboxModel.sequence_in_group.asc().nulls_first(),
                )
                .with_for_update()
            )
        )
        approval_ids = set(in_flight_approval_ids)
        allowed_rows: set[str] = set()
        for approval_id in approval_ids:
            group_id = f"approval:{approval_id}:resolution"
            rows = [row for row in pending if row.group_id == group_id]
            if len(rows) != 2:
                return True
            expected = (
                (1, EvidenceOperationKind.APPROVAL_RESOLUTION.value, 1),
                (2, "run_terminal", 0),
            )
            for row, (sequence, operation_kind, reserved_count) in zip(
                rows,
                expected,
                strict=True,
            ):
                payload = row.result_json
                if (
                    row.sequence_in_group != sequence
                    or row.operation_kind != operation_kind
                    or row.state != "result_persisted"
                    or not isinstance(payload, Mapping)
                    or payload.get("approval_id") != approval_id
                    or payload.get("resolution_status") != "approved"
                    or payload.get("run_status") != "pending"
                    or row.reserved_event_count != reserved_count
                ):
                    return True
                allowed_rows.add(row.id)
        return any(row.id not in allowed_rows for row in pending)

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
    "EventSequenceStateInvalid",
    "EventCapacityRepository",
    "EventCapacitySnapshot",
    "EvidenceOperationKind",
    "EvidenceOutboxRepository",
    "MAX_EVENT_SEQ",
    "OperationReservationSpec",
    "UsageSettlementClaim",
    "operation_event_capacity",
]
