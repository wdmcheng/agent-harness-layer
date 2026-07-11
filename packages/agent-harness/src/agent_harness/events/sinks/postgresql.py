"""使用应用 PostgreSQL 表的跨进程 CanonicalEvent sink。"""

from __future__ import annotations

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.events.sinks.base import EventSinkTerminalConflict
from agent_harness.events.types import CanonicalEvent, CanonicalEventType
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.models import AgentRunModel, CanonicalEventModel


class PostgreSQLEventSink:
    """锁定 run row 串行分配 seq，并由数据库唯一约束守住 terminal。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def write(self, event: CanonicalEvent) -> CanonicalEvent:
        try:
            async with self._storage.engine.begin() as connection:
                existing = await connection.execute(
                    select(CanonicalEventModel.envelope_json).where(
                        CanonicalEventModel.id == event.event_id
                    )
                )
                existing_envelope = existing.scalar_one_or_none()
                if existing_envelope is not None:
                    return CanonicalEvent.model_validate(existing_envelope)

                # 同一 application run row 是跨 API/worker 的 seq allocation mutex。
                locked_run = await connection.execute(
                    select(AgentRunModel.id)
                    .where(AgentRunModel.id == event.run_id)
                    .with_for_update()
                )
                if locked_run.scalar_one_or_none() is None:
                    raise LookupError(f"run not found: {event.run_id}")
                if event.terminal:
                    terminal = await connection.execute(
                        select(CanonicalEventModel.id).where(
                            CanonicalEventModel.run_id == event.run_id,
                            CanonicalEventModel.terminal.is_(True),
                        )
                    )
                    if terminal.first() is not None:
                        raise EventSinkTerminalConflict(
                            f"run already has terminal event: {event.run_id}"
                        )
                latest = await connection.execute(
                    select(func.max(CanonicalEventModel.seq)).where(
                        CanonicalEventModel.run_id == event.run_id
                    )
                )
                persisted = event.model_copy(update={"seq": (latest.scalar_one() or 0) + 1})
                await connection.execute(
                    insert(CanonicalEventModel).values(
                        id=persisted.event_id,
                        tenant_id=persisted.tenant_id,
                        run_id=persisted.run_id,
                        agent_id=persisted.agent_id,
                        event_type=persisted.event_type.value,
                        seq=persisted.seq,
                        terminal=persisted.terminal,
                        visibility=persisted.visibility,
                        payload_json=persisted.payload,
                        payload_ref=persisted.payload_ref,
                        request_id=persisted.request_id,
                        trace_id=persisted.trace_id,
                        envelope_json=persisted.to_payload(),
                    )
                )
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
                    return CanonicalEvent.model_validate(envelope)
            if event.terminal:
                raise EventSinkTerminalConflict(
                    f"run already has terminal event: {event.run_id}"
                ) from None
            raise

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
