"""使用应用 PostgreSQL 表的跨进程 CanonicalEvent sink。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.events.capacity import (
    UsageCapacitySettlement,
    stream_capacity_binding,
    usage_capacity_binding,
    validate_usage_capacity_outbox,
)
from agent_harness.events.serialization import canonical_event_bytes, validate_persisted_event_bytes
from agent_harness.events.sinks._postgresql_streaming import (
    validate_stream_event_capacity,
    validate_stream_usage_final,
)
from agent_harness.events.sinks.base import (
    DEFAULT_EVENT_PAGE_SIZE,
    MAX_EVENT_PAGE_BYTES,
    EventSinkTerminalConflict,
    validate_event_replay,
    validate_event_scope,
    validate_terminal_visibility,
)
from agent_harness.events.sinks.reader import EventPageAccumulator, validate_page_limits
from agent_harness.events.types import CanonicalEvent, CanonicalEventType
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.event_capacity_repositories import (
    EventCapacityExceeded,
    EventSequenceStateInvalid,
)
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.models import (
    AgentRunModel,
    CanonicalEventModel,
    RunEventCapacityModel,
    RunEvidenceOutboxModel,
)


class PostgreSQLEventSink:
    """锁定 run row 串行分配 seq，并由数据库唯一约束守住 terminal。"""

    manages_event_capacity = True

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        """绑定存储适配器；每次写入仍自行打开短事务以持有 run 级锁。"""

        self._storage = storage

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        """原子写入事件、推进容量高水位，并在重放时返回既有 envelope。

        该方法以数据库事务覆盖 event-id 去重、run/stream 串行化、容量消费、
        事件插入和可选 artifact materialize。任何校验或回调失败都会回滚，
        从而避免把可见事件与容量账本或 artifact 留在不同提交状态。
        """

        validate_event_scope(event)
        validate_terminal_visibility(event)
        canonical_event_bytes(event)
        try:
            async with self._storage.engine.begin() as connection:
                existing = await connection.execute(
                    select(CanonicalEventModel.envelope_json).where(
                        CanonicalEventModel.id == event.event_id
                    )
                )
                existing_envelope = existing.scalar_one_or_none()
                if existing_envelope is not None:
                    persisted = CanonicalEvent.model_validate(existing_envelope)
                    validate_event_replay(event, persisted)
                    return persisted

                if after_claim is not None and connection.dialect.name == "postgresql":
                    # 不同 tenant/run 也可能竞争同一 event-id。miss 后用事务级
                    # advisory lock 串行化，再查一次，确保只有获胜方 materialize
                    # artifact；已有记录的 fast path 不承担额外锁开销。
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:event_id, 0))"),
                        {"event_id": event.event_id},
                    )
                    existing = await connection.execute(
                        select(CanonicalEventModel.envelope_json).where(
                            CanonicalEventModel.id == event.event_id
                        )
                    )
                    existing_envelope = existing.scalar_one_or_none()
                    if existing_envelope is not None:
                        persisted = CanonicalEvent.model_validate(existing_envelope)
                        validate_event_replay(event, persisted)
                        return persisted

                if event.record_scope == "run":
                    # 同一 application run row 是跨 API/worker 的 seq allocation mutex。
                    locked_run = await connection.execute(
                        select(AgentRunModel.id, AgentRunModel.tenant_id, AgentRunModel.trace_id)
                        .where(AgentRunModel.id == event.run_id)
                        .with_for_update()
                    )
                    locked = locked_run.one_or_none()
                    if locked is None:
                        raise LookupError(f"run not found: {event.run_id}")
                    if locked.tenant_id != event.tenant_id:
                        raise ValueError("event tenant does not match persisted run")
                    if locked.trace_id != event.trace_id:
                        raise ValueError("event canonical trace does not match persisted run")
                elif connection.dialect.name == "postgresql":
                    # non-run stream 没有 AgentRun row 可锁。按 tenant + synthetic
                    # stream 取事务级锁，既不伪造 lineage，又让并发 seq 分配串行化。
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:stream, 0))"),
                        {"stream": f"{len(event.tenant_id)}:{event.tenant_id}{event.run_id}"},
                    )
                # non-run envelope 的 stream 可能与真实 run id 同名。它不获得
                # AgentRun ownership，但仍占用同一 tenant-scoped seq 空间，因此
                # 必须与 run writer 共锁容量行并推进同一 high-water mark。
                capacity = await connection.execute(
                    select(
                        RunEventCapacityModel.highest_persisted_seq,
                        RunEventCapacityModel.outstanding_reserved_event_count,
                        RunEventCapacityModel.terminal_reservation,
                    )
                    .where(
                        RunEventCapacityModel.run_id == event.run_id,
                        RunEventCapacityModel.tenant_id == event.tenant_id,
                    )
                    .with_for_update()
                )
                capacity_row = capacity.one_or_none()
                if event.record_scope == "run" and capacity_row is None:
                    raise RuntimeError("run event capacity is not initialized")
                terminal = await connection.execute(
                    select(CanonicalEventModel.id).where(
                        CanonicalEventModel.tenant_id == event.tenant_id,
                        CanonicalEventModel.stream_id == event.run_id,
                        CanonicalEventModel.terminal.is_(True),
                    )
                )
                if terminal.first() is not None:
                    raise EventSinkTerminalConflict(
                        f"run already has terminal event: {event.run_id}"
                    )
                latest = await connection.execute(
                    select(func.max(CanonicalEventModel.seq)).where(
                        CanonicalEventModel.tenant_id == event.tenant_id,
                        CanonicalEventModel.stream_id == event.run_id,
                    )
                )
                latest_seq = int(latest.scalar_one() or 0)
                if capacity_row is not None:
                    outstanding = capacity_row.outstanding_reserved_event_count
                    terminal_reservation = capacity_row.terminal_reservation
                    if (
                        capacity_row.highest_persisted_seq != latest_seq
                        or capacity_row.highest_persisted_seq < 0
                        or outstanding < 0
                        or terminal_reservation != 1
                        or capacity_row.highest_persisted_seq + outstanding + terminal_reservation
                        > MAX_EVENT_SEQ
                        or latest_seq == MAX_EVENT_SEQ
                    ):
                        raise EventSequenceStateInvalid
                persisted = event.model_copy(update={"seq": latest_seq + 1})
                if persisted.seq > MAX_EVENT_SEQ:
                    raise EventCapacityExceeded
                if persisted.seq == MAX_EVENT_SEQ and not persisted.terminal:
                    raise EventSequenceStateInvalid
                # seq 是 sink 在锁内分配的 envelope 字段；位数增长可能让调用方
                # 通过的 65536B 边界超限，必须用最终 shape 在任何 capacity/outbox
                # 更新和 INSERT 前重新校验。
                canonical_event_bytes(persisted)
                if capacity_row is not None:
                    # SQLAlchemy ``Row`` 的窄化不能跨越上面的 event 构造；在实际
                    # 消费分支重新取值，也让后续更新明确只依赖同一锁定快照。
                    outstanding = int(capacity_row.outstanding_reserved_event_count)
                    terminal_reservation = int(capacity_row.terminal_reservation)
                    usage_binding = usage_capacity_binding(event)
                    stream_binding = stream_capacity_binding(event)
                    if usage_binding is not None:
                        usage_outbox = await connection.execute(
                            select(
                                RunEvidenceOutboxModel.tenant_id,
                                RunEvidenceOutboxModel.run_id,
                                RunEvidenceOutboxModel.usage_call_id,
                                RunEvidenceOutboxModel.event_id,
                                RunEvidenceOutboxModel.operation_kind,
                                RunEvidenceOutboxModel.state,
                                RunEvidenceOutboxModel.reserved_event_count,
                                RunEvidenceOutboxModel.result_json,
                                RunEvidenceOutboxModel.error_code,
                            )
                            .where(
                                RunEvidenceOutboxModel.tenant_id == event.tenant_id,
                                RunEvidenceOutboxModel.usage_call_id == usage_binding.usage_call_id,
                            )
                            .with_for_update()
                        )
                        usage_outbox_row = usage_outbox.one_or_none()
                        usage_settlement = (
                            UsageCapacitySettlement(
                                tenant_id=str(usage_outbox_row[0]),
                                run_id=str(usage_outbox_row[1]),
                                usage_call_id=(
                                    str(usage_outbox_row[2])
                                    if usage_outbox_row[2] is not None
                                    else None
                                ),
                                event_id=str(usage_outbox_row[3]),
                                operation_kind=str(usage_outbox_row[4]),
                                state=str(usage_outbox_row[5]),
                                reserved_event_count=int(usage_outbox_row[6]),
                                result_json=usage_outbox_row[7],
                                error_code=(
                                    str(usage_outbox_row[8])
                                    if usage_outbox_row[8] is not None
                                    else None
                                ),
                            )
                            if usage_outbox_row is not None
                            else None
                        )
                        usage_reserved_event_count = validate_usage_capacity_outbox(
                            event=event,
                            binding=usage_binding,
                            outbox=usage_settlement,
                            expected_reserved_event_count=operation_event_capacity(
                                EvidenceOperationKind(usage_binding.operation_kind)
                            ),
                        )
                        if usage_binding.phase == "final":
                            started_event = await connection.execute(
                                select(CanonicalEventModel.id).where(
                                    CanonicalEventModel.id == usage_binding.started_event_id,
                                    CanonicalEventModel.tenant_id == event.tenant_id,
                                    CanonicalEventModel.stream_id == event.run_id,
                                )
                            )
                            if started_event.scalar_one_or_none() is None:
                                raise RuntimeError(
                                    "usage final event requires a persisted started event"
                                )
                            if usage_binding.started_event_id.startswith("usage-stream:"):
                                await validate_stream_usage_final(
                                    connection,
                                    event=event,
                                    usage_call_id=usage_binding.usage_call_id,
                                )
                    else:
                        usage_reserved_event_count = 0
                    if stream_binding is not None:
                        stream_reserved_event_count = await validate_stream_event_capacity(
                            connection,
                            event=event,
                            binding=stream_binding,
                        )
                    else:
                        stream_reserved_event_count = 0
                    outbox = await connection.execute(
                        select(
                            RunEvidenceOutboxModel.operation_kind,
                            RunEvidenceOutboxModel.reserved_event_count,
                        ).where(RunEvidenceOutboxModel.event_id == event.event_id)
                    )
                    outbox_row = outbox.one_or_none()
                    if event.record_scope == "run":
                        if event.terminal:
                            if terminal_reservation != 1 or outstanding != 0:
                                raise RuntimeError("pending evidence blocks terminal")
                            terminal_reservation = 0
                        elif usage_binding is not None:
                            if outstanding < usage_reserved_event_count:
                                raise RuntimeError("usage event has no capacity reservation")
                            outstanding -= usage_reserved_event_count
                        elif stream_binding is not None:
                            if outstanding < stream_reserved_event_count:
                                raise RuntimeError("stream event has no capacity reservation")
                            outstanding -= stream_reserved_event_count
                        elif outbox_row is not None and outbox_row.reserved_event_count:
                            if outstanding < outbox_row.reserved_event_count:
                                raise RuntimeError("outbox event has no capacity reservation")
                            outstanding -= outbox_row.reserved_event_count
                    if persisted.seq + outstanding + terminal_reservation > MAX_EVENT_SEQ:
                        raise EventCapacityExceeded
                    await connection.execute(
                        update(RunEventCapacityModel)
                        .where(
                            RunEventCapacityModel.run_id == event.run_id,
                            RunEventCapacityModel.tenant_id == event.tenant_id,
                        )
                        .values(
                            highest_persisted_seq=persisted.seq,
                            outstanding_reserved_event_count=outstanding,
                            terminal_reservation=terminal_reservation,
                        )
                    )
                await connection.execute(
                    insert(CanonicalEventModel).values(
                        id=persisted.event_id,
                        tenant_id=persisted.tenant_id,
                        run_id=(persisted.run_id if persisted.record_scope == "run" else None),
                        stream_id=persisted.run_id,
                        agent_id=persisted.agent_id,
                        event_type=persisted.event_type.value,
                        seq=persisted.seq,
                        terminal=persisted.terminal,
                        visibility=persisted.visibility,
                        payload_json=persisted.payload,
                        payload_ref=persisted.payload_ref,
                        request_id=persisted.request_id,
                        trace_id=persisted.trace_id,
                        record_scope=persisted.record_scope,
                        envelope_json=persisted.to_payload(),
                    )
                )
                # 先让 INSERT 的全部数据库约束通过，再在同一未提交事务内
                # materialize artifact。回调失败会回滚 event；INSERT 失败则不会
                # 留下无事件引用的 artifact。
                if after_claim is not None:
                    after_claim()
                return persisted
        except IntegrityError:
            # 并发 event-id 重放由唯一主键收敛；第二 terminal由 partial unique拒绝。
            async with self._storage.engine.connect() as connection:
                existing = await connection.execute(
                    select(CanonicalEventModel.envelope_json).where(
                        CanonicalEventModel.id == event.event_id
                    )
                )
                envelope = existing.scalar_one_or_none()
                if envelope is not None:
                    persisted = CanonicalEvent.model_validate(envelope)
                    validate_event_replay(event, persisted)
                    return persisted
            if event.terminal:
                raise EventSinkTerminalConflict(
                    f"run already has terminal event: {event.run_id}"
                ) from None
            raise

    async def write_after_claim(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None],
    ) -> CanonicalEvent:
        """把 artifact materialize 纳入 event-id advisory lock 与 DB 事务。"""

        return await self.write(event, after_claim=after_claim)

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        """读取指定真实 run 在游标后的全部事件，保持数据库序号排序。"""

        async with AsyncSession(self._storage.engine) as session:
            rows = await session.scalars(
                select(CanonicalEventModel)
                .where(
                    CanonicalEventModel.run_id == run_id,
                    CanonicalEventModel.seq > after_seq,
                )
                .order_by(CanonicalEventModel.seq.asc())
            )
            return [self._event_from_row(row) for row in rows.all()]

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int = 0,
        include_internal: bool = False,
        max_events: int = DEFAULT_EVENT_PAGE_SIZE,
        max_bytes: int = MAX_EVENT_PAGE_BYTES,
    ) -> list[CanonicalEvent]:
        """按可见性和事件/字节上限读取一页，避免未授权行影响分页结果。"""

        validate_page_limits(max_events=max_events, max_bytes=max_bytes)
        predicates = [
            CanonicalEventModel.run_id == run_id,
            CanonicalEventModel.seq > after_seq,
        ]
        if not include_internal:
            predicates.append(CanonicalEventModel.visibility == "public")
        async with AsyncSession(self._storage.engine) as session:
            rows = await session.stream_scalars(
                select(CanonicalEventModel)
                .where(*predicates)
                .order_by(CanonicalEventModel.seq.asc())
                .execution_options(yield_per=1)
            )
            page = EventPageAccumulator(
                max_events=max_events,
                max_bytes=max_bytes,
            )
            async for row in rows:
                # SQL WHERE 已先构造当前授权可见性视图；页内每个 row 随取随验，
                # 不调用 rows.all()，byte 上限命中后立即关闭当前 DB cursor。
                if not page.append(self._event_from_row(row)):
                    break
            await rows.close()
            return page.events

    async def contains_seq(
        self,
        *,
        run_id: str,
        seq: int,
        include_internal: bool = False,
    ) -> bool:
        """判断指定正序号是否位于当前调用者可见的事件流中。"""

        if seq <= 0:
            return False
        page = await self.read_page(
            run_id=run_id,
            after_seq=seq - 1,
            include_internal=include_internal,
            max_events=1,
        )
        return bool(page and page[0].seq == seq)

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool = False,
    ) -> CanonicalEvent | None:
        """返回指定 run 的可见终态事件，未找到时保持 ``None``。"""

        predicates = [
            CanonicalEventModel.run_id == run_id,
            CanonicalEventModel.terminal.is_(True),
        ]
        if not include_internal:
            predicates.append(CanonicalEventModel.visibility == "public")
        async with AsyncSession(self._storage.engine) as session:
            row = await session.scalar(select(CanonicalEventModel).where(*predicates))
            return self._event_from_row(row) if row is not None else None

    async def latest_seq(self, run_id: str) -> int:
        """查询指定 run 的最大已持久化序号，空结果返回零。"""

        async with self._storage.engine.connect() as connection:
            result = await connection.execute(
                select(func.max(CanonicalEventModel.seq)).where(
                    CanonicalEventModel.run_id == run_id
                )
            )
            return int(result.scalar_one() or 0)

    async def has_terminal(self, run_id: str) -> bool:
        """判断指定 run 是否已经提交终态，供上层恢复和写入门禁使用。"""

        async with self._storage.engine.connect() as connection:
            result = await connection.execute(
                select(CanonicalEventModel.id).where(
                    CanonicalEventModel.run_id == run_id,
                    CanonicalEventModel.terminal.is_(True),
                )
            )
            return result.first() is not None

    @staticmethod
    def _event_from_row(model: CanonicalEventModel) -> CanonicalEvent:
        """将数据库行恢复为 CanonicalEvent，并拒绝不完整的 non-run 旧记录。

        新行必须携带完整 envelope；仅为兼容迁移前真实 run 的旧行才从旧列重建，
        且绝不猜测缺失的 correlation、scope 或其他受信字段。
        """

        if model.envelope_json is not None:
            event = CanonicalEvent.model_validate(model.envelope_json)
            validate_persisted_event_bytes(event)
            return event
        # 0012前的 legacy row 只能恢复旧列已有字段，不能伪造缺失 correlation。
        if model.run_id is None:
            raise RuntimeError("non-run canonical event requires a persisted envelope")
        event = CanonicalEvent(
            event_id=model.id,
            tenant_id=model.tenant_id,
            run_id=model.run_id,
            agent_id=model.agent_id,
            event_type=CanonicalEventType(model.event_type),
            seq=model.seq,
            timestamp=model.created_at,
            payload=model.payload_json,
            payload_ref=model.payload_ref,
            terminal=model.terminal,
            visibility=model.visibility,
            request_id=model.request_id,
            trace_id=model.trace_id,
        )
        validate_persisted_event_bytes(event)
        return event


__all__ = ["PostgreSQLEventSink"]
