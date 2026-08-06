"""封闭有序证据组的持久化操作，保护 resolution 与 terminal 的发布顺序。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.models import RunEvidenceOutboxModel


def _required_text(item: Mapping[str, object], key: str) -> str:
    """读取组项目的必填非空文本字段，拒绝把缺失 ID 或类型错误带入 outbox。"""
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ordered evidence item requires non-empty {key}")
    return value


def _required_non_negative_int(item: Mapping[str, object], key: str) -> int:
    """读取非负整数容量或序号字段，显式拒绝 bool 等 Python 子类型混入计数。"""
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"ordered evidence item requires non-negative integer {key}")
    return value


def _optional_result(item: Mapping[str, object]) -> dict[str, Any] | None:
    """规范化可选结果对象，复制 mapping 以免调用方随后原地修改持久化意图。"""
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

    async def stage_reserved_group(
        self,
        *,
        tenant_id: str,
        run_id: str,
        group_id: str,
        items: Sequence[Mapping[str, object]],
    ) -> list[RunEvidenceOutboxModel]:
        """先冻结稳定event identity，正文只在对应业务结果确定后转为可发布。

        该接缝供必须先于handler/Context副作用预约容量的步骤使用；`reserved`
        行不能被EventBus发布或越过前序，崩溃时会继续围栏run terminal。
        """

        existing = await self.ordered_group(group_id=group_id)
        expected = [
            (
                _required_text(item, "event_id"),
                _required_text(item, "operation_kind"),
                _required_non_negative_int(item, "sequence_in_group"),
                _required_non_negative_int(item, "reserved_event_count"),
            )
            for item in items
        ]
        if existing:
            actual = [
                (
                    item.event_id,
                    item.operation_kind,
                    int(item.sequence_in_group or 0),
                    item.reserved_event_count,
                )
                for item in existing
            ]
            if actual != expected:
                raise ValueError("reserved evidence group does not match persisted items")
            return existing
        sequences = [item[2] for item in expected]
        if sequences != list(range(1, len(items) + 1)):
            raise ValueError("reserved evidence group sequence must be contiguous from one")
        models = [
            RunEvidenceOutboxModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                run_id=run_id,
                usage_call_id=None,
                event_id=event_id,
                operation_kind=operation_kind,
                state="reserved",
                result_json=None,
                reserved_event_count=reserved_event_count,
                group_id=group_id,
                sequence_in_group=sequence,
            )
            for event_id, operation_kind, sequence, reserved_event_count in expected
        ]
        self._session.add_all(models)
        await self._session.flush()
        return models

    async def persist_reserved_event(
        self,
        *,
        event_id: str,
        result: Mapping[str, object],
    ) -> None:
        """把单个已预约identity原子提升为exact可发布event intent。"""

        model = await self._session.scalar(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.event_id == event_id)
            .with_for_update()
        )
        if model is None:
            raise LookupError("reserved evidence event not found")
        snapshot = dict(result)
        if model.state == "reserved":
            model.result_json = snapshot
            model.state = "result_persisted"
            await self._session.flush()
            return
        if model.state in {"result_persisted", "published"} and model.result_json == snapshot:
            return
        raise ValueError("reserved evidence event conflicts with persisted result")

    async def ordered_group(self, *, group_id: str) -> list[RunEvidenceOutboxModel]:
        """按 sequence_in_group 读取完整证据组，供恢复与一致性比较使用。"""
        result = await self._session.scalars(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.group_id == group_id)
            .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
        )
        return list(result.all())

    async def mark_group_published(self, *, group_id: str) -> int:
        """将整个组标记为已发布；缺失组必须失败，不能把恢复误报为成功。"""
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

    async def mark_event_recovery_error(self, *, event_id: str, error_code: str) -> None:
        """保留原event intent与预约，只追加稳定恢复错误供人工处置。"""

        if not error_code:
            raise ValueError("event recovery error code must be non-empty")
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.event_id == event_id,
                    RunEvidenceOutboxModel.state.in_(("reserved", "result_persisted", "published")),
                )
                .values(error_code=error_code)
            ),
        )
        if changed.rowcount != 1:
            raise LookupError("evidence outbox event not found")

    async def update_group_result(
        self,
        *,
        group_id: str,
        result: Mapping[str, object],
    ) -> int:
        """只更新仍待发布组的共享结果，已发布组不可回写以维持证据不可变性。"""
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
