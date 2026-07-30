"""运行级事件高水位、终态槽位与副作用容量预约账本。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.models import AgentRunModel, RunEventCapacityModel

MAX_EVENT_SEQ = 2_147_483_647
EVIDENCE_OPERATION_REGISTRY_VERSION = "1"


class EvidenceOperationKind(StrEnum):
    """允许在副作用前预约 event 容量的封闭 operation kind。"""

    MODEL_USAGE = "model_usage"
    MODEL_STREAM = "model_stream"
    EMBEDDING_USAGE = "embedding_usage"
    APPROVAL_RESOLUTION = "approval_resolution"
    TOOL_INVOCATION = "tool_invocation"
    DELEGATION = "delegation"


@dataclass(frozen=True)
class OperationReservationSpec:
    """版本化 registry 中不可由业务输入缩小的最大前置事件数。"""

    version: str
    max_prerequisite_events: int


_OPERATION_EVENT_CAPACITY: Mapping[EvidenceOperationKind, OperationReservationSpec] = (
    MappingProxyType(
        {
            EvidenceOperationKind.MODEL_USAGE: OperationReservationSpec(
                version=EVIDENCE_OPERATION_REGISTRY_VERSION,
                max_prerequisite_events=2,
            ),
            EvidenceOperationKind.MODEL_STREAM: OperationReservationSpec(
                version=EVIDENCE_OPERATION_REGISTRY_VERSION,
                max_prerequisite_events=65,
            ),
            EvidenceOperationKind.EMBEDDING_USAGE: OperationReservationSpec(
                version=EVIDENCE_OPERATION_REGISTRY_VERSION,
                max_prerequisite_events=2,
            ),
            EvidenceOperationKind.APPROVAL_RESOLUTION: OperationReservationSpec(
                version=EVIDENCE_OPERATION_REGISTRY_VERSION,
                max_prerequisite_events=1,
            ),
            EvidenceOperationKind.TOOL_INVOCATION: OperationReservationSpec(
                version=EVIDENCE_OPERATION_REGISTRY_VERSION,
                max_prerequisite_events=3,
            ),
            EvidenceOperationKind.DELEGATION: OperationReservationSpec(
                version=EVIDENCE_OPERATION_REGISTRY_VERSION,
                max_prerequisite_events=3,
            ),
        }
    )
)


def _require_operation_kind(value: object) -> EvidenceOperationKind:
    """验证调用方传入的是封闭枚举成员，禁止从外部载荷伪造 operation kind。"""
    if not isinstance(value, EvidenceOperationKind):
        raise ValueError("unknown event operation kind")
    return value


def operation_event_capacity(operation_kind: EvidenceOperationKind) -> int:
    """只接受 typed kind；外部 payload 不能自报 event count。"""

    verified = _require_operation_kind(operation_kind)
    try:
        return _OPERATION_EVENT_CAPACITY[verified].max_prerequisite_events
    except KeyError as exc:
        raise ValueError("unknown event operation kind") from exc


class EventCapacityExceeded(RuntimeError):
    """副作用前无法安全预约 event seq。"""

    code = "event.sequence_exhausted"


class EventSequenceStateInvalid(RuntimeError):
    """event high-water、terminal 槽位或 owner 形状已损坏。"""

    code = "event.sequence_state_invalid"


@dataclass(frozen=True)
class EventCapacitySnapshot:
    """某个运行当前已持久化序列、未结算预约与终态槽位的只读快照。"""

    run_id: str
    tenant_id: str
    highest_persisted_seq: int
    outstanding_reserved_event_count: int
    terminal_reservation: int


class EventCapacityRepository:
    """以数据库锁/CAS 维护 run 级 high-water 与预约不变量。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前工作单元 session；容量变更与运行、证据写入必须同一事务提交。"""
        self._session = session

    async def ensure_run(self, *, tenant_id: str, run_id: str, terminal: bool = False) -> None:
        """初始化运行容量账本；已存在时保持幂等，不重置已持久化高水位。

        非终态运行预留一个最终事件槽位，终态历史记录则不再保留该槽位；这保证
        后续副作用预约不能挤占唯一 terminal sequence。
        """
        existing = await self._session.get(RunEventCapacityModel, run_id)
        if existing is not None:
            return
        self._session.add(
            RunEventCapacityModel(
                run_id=run_id,
                tenant_id=tenant_id,
                highest_persisted_seq=0,
                outstanding_reserved_event_count=0,
                terminal_reservation=0 if terminal else 1,
            )
        )
        await self._session.flush()

    async def snapshot(self, run_id: str) -> EventCapacitySnapshot:
        """读取单个运行的容量快照，缺失账本表示调用方尚未完成运行初始化。"""
        model = await self._session.get(RunEventCapacityModel, run_id)
        if model is None:
            raise LookupError(f"event capacity is not initialized: {run_id}")
        return EventCapacitySnapshot(
            run_id=model.run_id,
            tenant_id=model.tenant_id,
            highest_persisted_seq=model.highest_persisted_seq,
            outstanding_reserved_event_count=model.outstanding_reserved_event_count,
            terminal_reservation=model.terminal_reservation,
        )

    async def assert_sequence_state_valid(self, *, tenant_id: str, run_id: str) -> None:
        """只校验 durable sequence 形状，不消耗容量也不判断 exhaustion。"""

        run = await self._session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update()
        )
        capacity = await self._session.scalar(
            select(RunEventCapacityModel)
            .where(RunEventCapacityModel.run_id == run_id)
            .with_for_update()
        )
        if (
            run is None
            or capacity is None
            or run.tenant_id != tenant_id
            or capacity.tenant_id != tenant_id
            or run.status in {"completed", "failed", "cancelled"}
            or capacity.highest_persisted_seq < 0
            or capacity.outstanding_reserved_event_count < 0
            or capacity.terminal_reservation != 1
            or capacity.highest_persisted_seq
            + capacity.outstanding_reserved_event_count
            + capacity.terminal_reservation
            > MAX_EVENT_SEQ
        ):
            raise EventSequenceStateInvalid

    async def exists(self, run_id: str) -> bool:
        """区分 application run stream 与没有容量账本的 non-run telemetry。"""

        return await self._session.get(RunEventCapacityModel, run_id) is not None

    async def reconcile_local_prefix(self, *, run_id: str, highest_persisted_seq: int) -> None:
        """在新预约前接管 legacy JSONL 前缀；未决副作用存在时拒绝猜测。"""

        model = await self._session.get(RunEventCapacityModel, run_id)
        if model is None:
            raise RuntimeError("run event capacity is not initialized")
        if highest_persisted_seq < model.highest_persisted_seq:
            raise RuntimeError("local event high-water mark is invalid")
        if highest_persisted_seq == model.highest_persisted_seq:
            return
        if model.outstanding_reserved_event_count:
            raise RuntimeError("pending evidence blocks local prefix reconciliation")
        if highest_persisted_seq + model.terminal_reservation > MAX_EVENT_SEQ:
            # legacy/direct-write 前缀已经侵占 terminal 预留槽，属于 durable
            # 状态损坏而非普通容量不足；调用方必须 fail closed 并人工处置。
            raise EventSequenceStateInvalid
        model.highest_persisted_seq = highest_persisted_seq
        await self._session.flush()

    async def reserve(self, *, run_id: str, operation_kind: EvidenceOperationKind) -> int:
        """只从封闭 registry 取最大预约数，拒绝业务输入自报容量。"""

        required = operation_event_capacity(operation_kind)
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEventCapacityModel)
                .where(
                    RunEventCapacityModel.run_id == run_id,
                    RunEventCapacityModel.highest_persisted_seq >= 0,
                    RunEventCapacityModel.outstanding_reserved_event_count >= 0,
                    RunEventCapacityModel.terminal_reservation == 1,
                    RunEventCapacityModel.highest_persisted_seq
                    + RunEventCapacityModel.outstanding_reserved_event_count
                    <= MAX_EVENT_SEQ - required - RunEventCapacityModel.terminal_reservation,
                )
                .values(
                    outstanding_reserved_event_count=(
                        RunEventCapacityModel.outstanding_reserved_event_count + required
                    )
                )
            ),
        )
        if changed.rowcount == 1:
            return required
        model = await self._session.scalar(
            select(RunEventCapacityModel).where(RunEventCapacityModel.run_id == run_id)
        )
        if model is None:
            raise LookupError(f"event capacity is not initialized: {run_id}")
        if (
            model.highest_persisted_seq < 0
            or model.outstanding_reserved_event_count < 0
            or model.terminal_reservation != 1
            or model.highest_persisted_seq
            + model.outstanding_reserved_event_count
            + model.terminal_reservation
            > MAX_EVENT_SEQ
        ):
            raise EventSequenceStateInvalid
        raise EventCapacityExceeded

    async def settle(self, *, run_id: str, reserved_event_count: int, consumed: int) -> None:
        """结算一次已预约操作，释放未消费槽位并推进真实已持久化高水位。

        ``consumed`` 必须在预约范围内；任何少于预约的事件数都只释放余额，不能
        让调用方通过传入负数或超额数目破坏容量不变量。
        """
        if consumed < 0 or consumed > reserved_event_count:
            raise ValueError("invalid event reservation settlement")
        model = await self._session.get(RunEventCapacityModel, run_id)
        if model is None or model.outstanding_reserved_event_count < reserved_event_count:
            raise RuntimeError("event reservation state is invalid")
        model.outstanding_reserved_event_count -= reserved_event_count
        model.highest_persisted_seq += consumed
        await self._session.flush()

    async def release(self, *, run_id: str, reserved_event_count: object) -> None:
        """只释放从未产生事件的预约，并在仓储边界收窄运行时输入。"""

        if (
            isinstance(reserved_event_count, bool)
            or not isinstance(reserved_event_count, int)
            or reserved_event_count <= 0
        ):
            raise ValueError("event reservation release must be a positive integer")
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEventCapacityModel)
                .where(
                    RunEventCapacityModel.run_id == run_id,
                    RunEventCapacityModel.highest_persisted_seq >= 0,
                    RunEventCapacityModel.outstanding_reserved_event_count >= reserved_event_count,
                    RunEventCapacityModel.terminal_reservation == 1,
                )
                .values(
                    outstanding_reserved_event_count=(
                        RunEventCapacityModel.outstanding_reserved_event_count
                        - reserved_event_count
                    )
                )
            ),
        )
        if changed.rowcount == 1:
            return
        model = await self._session.scalar(
            select(RunEventCapacityModel).where(RunEventCapacityModel.run_id == run_id)
        )
        if model is None:
            raise LookupError(f"event capacity is not initialized: {run_id}")
        raise RuntimeError("event reservation state is invalid")

    async def publish_terminal(self, *, run_id: str, seq: int) -> None:
        """消耗唯一终态槽位并写入最终序列，前置证据尚未结算时严格拒绝。

        终态序列必须大于当前高水位且不超过上限；该检查与 outbox 发布顺序共同
        保证公共 terminal 不会早于需要审计的副作用证据。
        """
        model = await self._session.get(RunEventCapacityModel, run_id)
        if model is None or model.terminal_reservation != 1:
            raise RuntimeError("terminal reservation is unavailable")
        if model.outstanding_reserved_event_count:
            raise RuntimeError("pending evidence blocks terminal")
        if seq <= model.highest_persisted_seq or seq > MAX_EVENT_SEQ:
            raise EventCapacityExceeded
        model.terminal_reservation = 0
        model.highest_persisted_seq = seq
        await self._session.flush()

    async def record_local_published(
        self,
        *,
        run_id: str,
        reserved_event_count: int,
        highest_persisted_seq: int,
        terminal: bool = False,
    ) -> None:
        """用 JSONL sink 返回的真实 seq 对账，不能按事件数量猜 high-water。"""

        model = await self._session.get(RunEventCapacityModel, run_id)
        if model is None or model.outstanding_reserved_event_count < reserved_event_count:
            raise RuntimeError("event reservation state is invalid")
        if (
            highest_persisted_seq < model.highest_persisted_seq
            or highest_persisted_seq > MAX_EVENT_SEQ
        ):
            raise RuntimeError("local event high-water mark is invalid")
        if terminal:
            if model.terminal_reservation != 1:
                raise RuntimeError("terminal reservation is unavailable")
            model.terminal_reservation = 0
        model.outstanding_reserved_event_count -= reserved_event_count
        model.highest_persisted_seq = highest_persisted_seq
        await self._session.flush()

    async def assert_terminal_publishable(self, *, run_id: str) -> None:
        """锁定容量行并确认所有前置 evidence 已结算。"""

        model = await self._session.scalar(
            select(RunEventCapacityModel)
            .where(RunEventCapacityModel.run_id == run_id)
            .with_for_update()
        )
        if model is None or model.terminal_reservation != 1:
            raise RuntimeError("terminal reservation is unavailable")
        if model.outstanding_reserved_event_count:
            raise RuntimeError("pending evidence blocks terminal")

    async def record_local_event(
        self,
        *,
        run_id: str,
        seq: int,
        reserved_event_count: int = 0,
        terminal: bool = False,
    ) -> None:
        """把 JSONL 实际 seq 与预约消费写回 SQLite 容量账本，重放幂等。"""

        model = await self._session.get(RunEventCapacityModel, run_id)
        if model is None:
            raise RuntimeError("run event capacity is not initialized")
        if (
            model.highest_persisted_seq < 0
            or model.outstanding_reserved_event_count < 0
            or model.terminal_reservation not in {0, 1}
            or model.highest_persisted_seq
            + model.outstanding_reserved_event_count
            + model.terminal_reservation
            > MAX_EVENT_SEQ
        ):
            raise EventSequenceStateInvalid
        if seq <= model.highest_persisted_seq:
            # 同一 event-id 重放可能发生在 capacity 已提交但调用方未收到确认后。
            # high-water 只能按连续 seq 前进，因此落在已提交前缀内即为幂等成功；
            # 不能再次扣减预约或 terminal reservation。
            return
        if seq != model.highest_persisted_seq + 1 or seq > MAX_EVENT_SEQ:
            raise EventSequenceStateInvalid
        if seq == MAX_EVENT_SEQ and not terminal:
            raise EventSequenceStateInvalid
        outstanding = model.outstanding_reserved_event_count
        if reserved_event_count:
            if outstanding < reserved_event_count:
                raise EventSequenceStateInvalid
            outstanding -= reserved_event_count
        terminal_reservation = model.terminal_reservation
        if terminal:
            if outstanding:
                raise RuntimeError("pending evidence blocks terminal")
            if terminal_reservation != 1:
                raise EventSequenceStateInvalid
            terminal_reservation = 0
        if seq + outstanding + terminal_reservation > MAX_EVENT_SEQ:
            raise EventCapacityExceeded
        model.outstanding_reserved_event_count = outstanding
        model.terminal_reservation = terminal_reservation
        model.highest_persisted_seq = seq
        await self._session.flush()


__all__ = [
    "EVIDENCE_OPERATION_REGISTRY_VERSION",
    "EventCapacityExceeded",
    "EventSequenceStateInvalid",
    "EventCapacityRepository",
    "EventCapacitySnapshot",
    "EvidenceOperationKind",
    "MAX_EVENT_SEQ",
    "OperationReservationSpec",
    "operation_event_capacity",
]
