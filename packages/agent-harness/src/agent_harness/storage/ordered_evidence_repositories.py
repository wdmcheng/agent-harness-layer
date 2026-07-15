"""封闭有序 evidence group 的持久化操作。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.models import RunEvidenceOutboxModel


def _required_text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ordered evidence item requires non-empty {key}")
    return value


def _required_non_negative_int(item: Mapping[str, object], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"ordered evidence item requires non-negative integer {key}")
    return value


def _optional_result(item: Mapping[str, object]) -> dict[str, Any] | None:
    value = item.get("result")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("ordered evidence item result must be a mapping or null")
    return dict(cast(Mapping[str, Any], value))


class OrderedEvidenceRepositoryMixin:
    """为 evidence outbox repository 提供有序 group 的原子写入与恢复。"""

    _session: AsyncSession

    async def stage_ordered_group(
        self,
        *,
        tenant_id: str,
        run_id: str,
        group_id: str,
        items: Sequence[Mapping[str, object]],
    ) -> list[RunEvidenceOutboxModel]:
        """同一事务写入封闭、有序的 resolution/terminal evidence group。"""

        existing = await self.ordered_group(group_id=group_id)
        if existing:
            expected = [
                (
                    _required_text(item, "event_id"),
                    _required_text(item, "operation_kind"),
                    _required_non_negative_int(item, "sequence_in_group"),
                    _required_non_negative_int(item, "reserved_event_count"),
                    _optional_result(item),
                )
                for item in items
            ]
            actual = [
                (
                    item.event_id,
                    item.operation_kind,
                    int(item.sequence_in_group or 0),
                    item.reserved_event_count,
                    item.result_json,
                )
                for item in existing
            ]
            if actual != expected:
                raise ValueError("ordered evidence group does not match persisted items")
            return existing
        sequences = [_required_non_negative_int(item, "sequence_in_group") for item in items]
        if sequences != list(range(1, len(items) + 1)):
            raise ValueError("ordered evidence group sequence must be contiguous from one")
        models = [
            RunEvidenceOutboxModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                run_id=run_id,
                usage_call_id=None,
                event_id=_required_text(item, "event_id"),
                operation_kind=_required_text(item, "operation_kind"),
                state="result_persisted",
                result_json=_optional_result(item),
                reserved_event_count=_required_non_negative_int(item, "reserved_event_count"),
                group_id=group_id,
                sequence_in_group=_required_non_negative_int(item, "sequence_in_group"),
            )
            for item in items
        ]
        self._session.add_all(models)
        await self._session.flush()
        return models

    async def ordered_group(self, *, group_id: str) -> list[RunEvidenceOutboxModel]:
        result = await self._session.scalars(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.group_id == group_id)
            .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
        )
        return list(result.all())

    async def mark_group_published(self, *, group_id: str) -> int:
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.group_id == group_id)
                .values(state="published")
            ),
        )
        if changed.rowcount < 1:
            raise LookupError("ordered evidence group not found")
        return int(changed.rowcount)

    async def update_group_result(
        self,
        *,
        group_id: str,
        result: Mapping[str, object],
    ) -> int:
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.group_id == group_id,
                    RunEvidenceOutboxModel.state == "result_persisted",
                )
                .values(result_json=dict(result))
            ),
        )
        if changed.rowcount < 1:
            raise LookupError("ordered evidence group is not pending")
        return int(changed.rowcount)


__all__ = ["OrderedEvidenceRepositoryMixin"]
