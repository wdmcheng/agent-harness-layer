"""使用应用 PostgreSQL 表的跨进程 CanonicalEvent sink。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.events.sinks.base import (
    EventSinkTerminalConflict,
    validate_event_replay,
    validate_event_scope,
    validate_terminal_visibility,
)
from agent_harness.events.types import CanonicalEvent, CanonicalEventType
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.models import AgentRunModel, CanonicalEventModel


class PostgreSQLEventSink:
    """锁定 run row 串行分配 seq，并由数据库唯一约束守住 terminal。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        validate_event_scope(event)
        validate_terminal_visibility(event)
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
                if event.terminal:
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
                persisted = event.model_copy(update={"seq": (latest.scalar_one() or 0) + 1})
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

    async def latest_seq(self, run_id: str) -> int:
        async with self._storage.engine.connect() as connection:
            result = await connection.execute(
                select(func.max(CanonicalEventModel.seq)).where(
                    CanonicalEventModel.run_id == run_id
                )
            )
            return int(result.scalar_one() or 0)

    async def has_terminal(self, run_id: str) -> bool:
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
        if model.envelope_json is not None:
            return CanonicalEvent.model_validate(model.envelope_json)
        # 0012前的 legacy row 只能恢复旧列已有字段，不能伪造缺失 correlation。
        if model.run_id is None:
            raise RuntimeError("non-run canonical event requires a persisted envelope")
        return CanonicalEvent(
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


__all__ = ["PostgreSQLEventSink"]
