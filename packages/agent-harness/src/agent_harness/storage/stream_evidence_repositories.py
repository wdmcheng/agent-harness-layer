"""普通文本流的稳定 identity 与同事务 evidence 占位仓储。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.event_capacity_repositories import (
    EventCapacityRepository,
    EvidenceOperationKind,
)
from agent_harness.storage.models import (
    AgentRunModel,
    RunEventCapacityModel,
    RunEvidenceOutboxModel,
)

if TYPE_CHECKING:
    from agent_harness.events.types import CanonicalEvent


_USAGE_CALL_ID = re.compile(r"[0-9a-f]{64}\Z")
_STREAM_ITEM_COUNT = 65


class _StreamBinding(TypedDict):
    """占位行内可持久化、可逐值复核的 stream 绑定。"""

    usage_call_id: str
    kind: Literal["delta", "completed"]
    ordinal: int


class _StreamItemValues(TypedDict):
    """单条 stream 占位的封闭 bulk-insert 形状。"""

    id: str
    tenant_id: str
    run_id: str
    usage_call_id: None
    event_id: str
    operation_kind: str
    state: str
    reserved_event_count: int
    result_json: dict[str, _StreamBinding]
    group_id: str
    sequence_in_group: int


def _require_usage_call_id(usage_call_id: object) -> str:
    """只接受调用身份的 canonical SHA-256 根，避免租户等可变输入撑大数据库键。"""

    if not isinstance(usage_call_id, str) or _USAGE_CALL_ID.fullmatch(usage_call_id) is None:
        raise ValueError("stream usage call id must be 64 lowercase hex characters")
    return usage_call_id


def stream_group_id(usage_call_id: object) -> str:
    """返回一个调用唯一、可恢复且受数据库列宽约束的 stream group id。"""

    return f"model-stream:{_require_usage_call_id(usage_call_id)}"


def stream_delta_event_id(usage_call_id: object, ordinal: object) -> str:
    """返回第 1..64 个 delta 的稳定事件标识。"""

    root = _require_usage_call_id(usage_call_id)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= 64:
        raise ValueError("stream delta ordinal must be between 1 and 64")
    return f"model-stream:{root}:d:{ordinal}"


def stream_completed_event_id(usage_call_id: object) -> str:
    """返回唯一 completed 事件标识。"""

    return f"model-stream:{_require_usage_call_id(usage_call_id)}:c"


def stream_usage_event_id(usage_call_id: object, phase: object) -> str:
    """返回 stream 专用 started/final usage identity，不把旧行静默迁移。"""

    root = _require_usage_call_id(usage_call_id)
    if not isinstance(phase, str):
        raise ValueError("stream usage phase must be started or final")
    suffix = {"started": "s", "final": "f"}.get(phase)
    if suffix is None:
        raise ValueError("stream usage phase must be started or final")
    return f"usage-stream:{root}:{suffix}"


@dataclass(frozen=True)
class StreamEvidenceClaim:
    """一次 65 槽 stream claim 的持久化结果。"""

    created: bool
    items: tuple[RunEvidenceOutboxModel, ...]


def require_complete_settled_predecessors(
    *,
    current_sequence: int,
    predecessors: Sequence[tuple[int | None, str]],
) -> None:
    """要求前驱恰好覆盖 ``1..current-1``，且每行都处于已结算状态。"""

    expected_sequences = list(range(1, current_sequence))
    actual_sequences = [sequence for sequence, _state in predecessors]
    if actual_sequences != expected_sequences or any(
        state not in {"published", "cancelled"} for _sequence, state in predecessors
    ):
        raise LookupError("ordered evidence predecessor is missing or not settled")


def _stream_item_values(
    *,
    tenant_id: str,
    run_id: str,
    usage_call_id: str,
) -> list[_StreamItemValues]:
    """构造封闭 64 delta + 1 completed 占位，不接收业务方自报容量。"""

    group_id = stream_group_id(usage_call_id)
    values: list[_StreamItemValues] = []
    for ordinal in range(1, 65):
        values.append(
            {
                "id": str(uuid4()),
                "tenant_id": tenant_id,
                "run_id": run_id,
                "usage_call_id": None,
                "event_id": stream_delta_event_id(usage_call_id, ordinal),
                "operation_kind": EvidenceOperationKind.MODEL_STREAM.value,
                "state": "started",
                "reserved_event_count": 1,
                "result_json": {
                    "stream": {
                        "usage_call_id": usage_call_id,
                        "kind": "delta",
                        "ordinal": ordinal,
                    }
                },
                "group_id": group_id,
                "sequence_in_group": ordinal,
            }
        )
    values.append(
        {
            "id": str(uuid4()),
            "tenant_id": tenant_id,
            "run_id": run_id,
            "usage_call_id": None,
            "event_id": stream_completed_event_id(usage_call_id),
            "operation_kind": EvidenceOperationKind.MODEL_STREAM.value,
            "state": "started",
            "reserved_event_count": 1,
            "result_json": {
                "stream": {
                    "usage_call_id": usage_call_id,
                    "kind": "completed",
                    "ordinal": 65,
                }
            },
            "group_id": group_id,
            "sequence_in_group": 65,
        }
    )
    return values


def _validate_stream_group(
    items: list[RunEvidenceOutboxModel],
    *,
    tenant_id: str,
    run_id: str,
    usage_call_id: str,
) -> None:
    """重放只接受完整且逐字段等价的组，部分组或串号必须人工处置。"""

    expected = _stream_item_values(
        tenant_id=tenant_id,
        run_id=run_id,
        usage_call_id=usage_call_id,
    )
    if len(items) != _STREAM_ITEM_COUNT:
        raise ValueError("stream evidence group must contain exactly 65 items")
    for item, wanted in zip(items, expected, strict=True):
        persisted_stream = (
            item.result_json.get("stream") if isinstance(item.result_json, Mapping) else None
        )
        if (
            item.tenant_id != tenant_id
            or item.run_id != run_id
            or item.usage_call_id is not None
            or item.event_id != wanted["event_id"]
            or item.operation_kind != EvidenceOperationKind.MODEL_STREAM.value
            or item.state not in {"started", "result_persisted", "published", "cancelled"}
            or item.reserved_event_count != 1
            or item.group_id != wanted["group_id"]
            or item.sequence_in_group != wanted["sequence_in_group"]
            or persisted_stream != wanted["result_json"]["stream"]
        ):
            raise ValueError("stream evidence group does not match persisted binding")


class StreamEvidenceRepositoryMixin:
    """复用 evidence outbox 的 UoW session 原子声明整个 stream 容量。"""

    _session: AsyncSession

    async def claim_stream(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
    ) -> StreamEvidenceClaim:
        """原子竞争完整占位；只有完整插入胜者才预约 65 个事件槽。"""

        _require_usage_call_id(usage_call_id)
        run = await self._session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update()
        )
        if run is None:
            raise LookupError(f"stream run is not persisted: {run_id}")
        if run.tenant_id != tenant_id:
            raise ValueError("stream tenant does not own run")
        if run.status in {"completed", "failed", "cancelled"}:
            raise RuntimeError("terminal run does not accept a new stream")
        capacity_tenant = await self._session.scalar(
            select(RunEventCapacityModel.tenant_id).where(RunEventCapacityModel.run_id == run_id)
        )
        if capacity_tenant is None:
            raise LookupError(f"event capacity is not initialized: {run_id}")
        if capacity_tenant != tenant_id:
            raise ValueError("stream tenant does not own run")

        group_id = stream_group_id(usage_call_id)
        existing = await self._stream_group(group_id)
        if existing:
            _validate_stream_group(
                existing,
                tenant_id=tenant_id,
                run_id=run_id,
                usage_call_id=usage_call_id,
            )
            return StreamEvidenceClaim(created=False, items=tuple(existing))

        values = _stream_item_values(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
        )
        dialect_name = self._session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = postgresql.insert(RunEvidenceOutboxModel).values(values)
        elif dialect_name == "sqlite":
            statement = sqlite.insert(RunEvidenceOutboxModel).values(values)
        else:  # pragma: no cover - 当前产品矩阵只支持 SQLite/PostgreSQL
            raise RuntimeError(f"unsupported stream evidence dialect: {dialect_name}")
        inserted_ids = list(
            (
                await self._session.scalars(
                    statement.on_conflict_do_nothing().returning(RunEvidenceOutboxModel.id)
                )
            ).all()
        )
        if len(inserted_ids) == _STREAM_ITEM_COUNT:
            reserved = await EventCapacityRepository(self._session).reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_STREAM,
            )
            if reserved != _STREAM_ITEM_COUNT:  # pragma: no cover - registry 自检兜底
                raise RuntimeError("stream capacity registry is inconsistent")
            created = await self._stream_group(group_id)
            _validate_stream_group(
                created,
                tenant_id=tenant_id,
                run_id=run_id,
                usage_call_id=usage_call_id,
            )
            return StreamEvidenceClaim(created=True, items=tuple(created))
        if inserted_ids:
            # 单条 bulk INSERT 只应全赢或全输；部分冲突表示 durable group 已损坏。
            raise RuntimeError("stream evidence group was only partially claimed")
        replay = await self._stream_group(group_id)
        _validate_stream_group(
            replay,
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
        )
        return StreamEvidenceClaim(created=False, items=tuple(replay))

    async def persist_stream_event(
        self,
        event: CanonicalEvent,
    ) -> RunEvidenceOutboxModel:
        """把一个完整且受信的 stream event intent 固化到预建占位。"""

        model = await self._session.scalar(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.event_id == event.event_id)
            .with_for_update()
        )
        if model is None:
            raise LookupError("stream evidence placeholder not found")
        normalized = _validated_stream_event_result(model=model, event=event)
        if model.state in {"result_persisted", "published"}:
            if model.result_json != normalized:
                raise RuntimeError("persisted stream event conflict")
            return model
        if model.state != "started":
            raise RuntimeError(f"stream event cannot persist from state: {model.state}")
        model.result_json = normalized
        model.state = "result_persisted"
        await self._session.flush()
        return model

    async def cancel_unused_stream(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        used_delta_count: object,
        keep_completed: object,
    ) -> int:
        """取消从未固化的尾部占位，并等量释放 outstanding 而不回退 high-water。"""

        _require_usage_call_id(usage_call_id)
        if (
            isinstance(used_delta_count, bool)
            or not isinstance(used_delta_count, int)
            or not 0 <= used_delta_count <= 64
        ):
            raise ValueError("used stream delta count must be between 0 and 64")
        if not isinstance(keep_completed, bool):
            raise ValueError("keep_completed must be boolean")
        group_id = stream_group_id(usage_call_id)
        items = list(
            (
                await self._session.scalars(
                    select(RunEvidenceOutboxModel)
                    .where(RunEvidenceOutboxModel.group_id == group_id)
                    .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
                    .with_for_update()
                )
            ).all()
        )
        _validate_stream_group(
            items,
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
        )
        if any(item.state != "published" for item in items[:used_delta_count]):
            raise RuntimeError("used stream delta prefix is not fully published")
        targets = items[used_delta_count:64]
        if not keep_completed:
            targets = [*targets, items[64]]
        if any(item.state not in {"started", "cancelled"} for item in targets):
            raise RuntimeError("stream placeholder with durable result cannot be cancelled")
        target_ids = [item.id for item in targets if item.state == "started"]
        if not target_ids:
            return 0
        changed = cast(
            CursorResult[tuple[()]],
            await self._session.execute(
                update(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.id.in_(target_ids),
                    RunEvidenceOutboxModel.state == "started",
                )
                .values(state="cancelled")
            ),
        )
        released = int(changed.rowcount)
        if released != len(target_ids):
            raise RuntimeError("stream placeholder cancellation lost its row lock")
        await EventCapacityRepository(self._session).release(
            run_id=run_id,
            reserved_event_count=released,
        )
        return released

    async def ensure_stream_settled_before_usage_final(
        self,
        *,
        usage_call_id: str,
        outcome: str,
    ) -> None:
        """在 usage final 前锁定并验证同一调用的跨组 stream 前驱。"""

        _require_usage_call_id(usage_call_id)
        if outcome not in {"completed", "cancelled", "failed"}:
            raise ValueError("stream usage outcome is invalid")
        items = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.group_id == stream_group_id(usage_call_id))
                .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
                .with_for_update()
            )
        )
        if not items:
            raise LookupError("stream evidence group not found")
        _validate_stream_group(
            items,
            tenant_id=items[0].tenant_id,
            run_id=items[0].run_id,
            usage_call_id=usage_call_id,
        )
        completed_state = items[64].state
        if outcome == "completed" and completed_state != "published":
            raise LookupError("completed stream evidence is not published")
        if outcome != "completed" and completed_state != "cancelled":
            raise LookupError("interrupted stream completed placeholder is not cancelled")
        if any(item.state not in {"published", "cancelled"} for item in items):
            raise LookupError("stream evidence predecessor is not settled")

    async def _stream_group(self, group_id: str) -> list[RunEvidenceOutboxModel]:
        """在当前事务按固定序号读取一组 stream 占位。"""

        result = await self._session.scalars(
            select(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.group_id == group_id)
            .order_by(RunEvidenceOutboxModel.sequence_in_group.asc())
        )
        return list(result.all())


def _validated_stream_event_result(
    *,
    model: RunEvidenceOutboxModel,
    event: object,
) -> dict[str, object]:
    """验证 event 与预建 identity/payload 逐值相符，并返回可恢复 intent。"""

    # 局部 import 避免 storage 初始化时触发 events -> storage 的模块环。
    from agent_harness.events.types import CanonicalEvent, CanonicalEventType

    if not isinstance(event, CanonicalEvent):
        raise TypeError("stream event must be a CanonicalEvent")
    stream = model.result_json.get("stream") if isinstance(model.result_json, Mapping) else None
    if not isinstance(stream, Mapping):
        raise RuntimeError("stream placeholder is missing its durable binding")
    stream_mapping = cast(Mapping[str, object], stream)
    usage_call_id = stream_mapping.get("usage_call_id")
    kind = stream_mapping.get("kind")
    ordinal = stream_mapping.get("ordinal")
    if (
        not isinstance(usage_call_id, str)
        or not isinstance(kind, str)
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
    ):
        raise RuntimeError("stream placeholder binding is invalid")
    if (
        model.operation_kind != EvidenceOperationKind.MODEL_STREAM.value
        or model.reserved_event_count != 1
        or event.tenant_id != model.tenant_id
        or event.run_id != model.run_id
        or event.event_id != model.event_id
        or event.record_scope != "run"
        or event.visibility != "public"
        or event.terminal
        or event.payload_ref is not None
        or event.payload_checksum is not None
        or event.raw_event_ref is not None
    ):
        raise ValueError("stream event envelope does not match its durable placeholder")
    correlation = {"usage_call_id": usage_call_id}
    payload = event.payload
    payload_mapping = cast(Mapping[str, object], payload) if isinstance(payload, Mapping) else None
    attempt = payload_mapping.get("attempt") if payload_mapping is not None else None
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("stream event attempt must be a positive global ordinal")
    if kind == "delta":
        text = payload_mapping.get("text") if payload_mapping is not None else None
        expected_payload = {
            "correlation": correlation,
            "attempt": attempt,
            "chunk_ordinal": ordinal,
            "text": text,
        }
        if (
            event.event_type != CanonicalEventType.MODEL_OUTPUT_DELTA
            or event.event_id != stream_delta_event_id(usage_call_id, ordinal)
            or not isinstance(text, str)
            or not text
            or len(text.encode("utf-8")) > 4096
            or payload != expected_payload
        ):
            raise ValueError("stream delta does not match its durable placeholder")
    elif kind == "completed":
        expected_keys = {
            "correlation",
            "attempt",
            "chunk_count",
            "text_utf8_bytes",
            "text_sha256",
        }
        if payload_mapping is None:
            raise ValueError("stream completed requires a payload")
        chunk_count = payload_mapping.get("chunk_count")
        text_utf8_bytes = payload_mapping.get("text_utf8_bytes")
        text_sha256 = payload_mapping.get("text_sha256")
        if (
            event.event_type != CanonicalEventType.MODEL_OUTPUT_COMPLETED
            or event.event_id != stream_completed_event_id(usage_call_id)
            or set(payload_mapping) != expected_keys
            or payload_mapping.get("correlation") != correlation
            or payload_mapping.get("attempt") != attempt
            or isinstance(chunk_count, bool)
            or not isinstance(chunk_count, int)
            or not 0 <= chunk_count <= 64
            or isinstance(text_utf8_bytes, bool)
            or not isinstance(text_utf8_bytes, int)
            or text_utf8_bytes < 0
            or not isinstance(text_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", text_sha256) is None
        ):
            raise ValueError("stream completed does not match its durable placeholder")
    else:
        raise RuntimeError("stream placeholder kind is invalid")
    return {"stream": dict(stream_mapping), "event": event.to_payload()}


__all__ = [
    "require_complete_settled_predecessors",
    "StreamEvidenceClaim",
    "StreamEvidenceRepositoryMixin",
    "stream_completed_event_id",
    "stream_delta_event_id",
    "stream_group_id",
    "stream_usage_event_id",
]
