"""Model usage local capacity 合同共享的持久化夹具。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import update

from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    canonical_event_bytes,
)
from agent_harness.local_state import register_local_state_file
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage
from agent_harness.storage.models import RunEventCapacityModel


async def seed_run(storage: SQLAlchemyStorage, *, request_id: str | None = None) -> str:
    """创建带 terminal reservation 的稳定 tenant/run。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            execution_context=(
                {
                    "identity": {
                        "tenant_id": "tenant-a",
                        "user_id": "user-a",
                        "roles": [],
                    },
                    "request_id": request_id,
                    "trace_id": "trace-a",
                }
                if request_id is not None
                else None
            ),
        )
        await uow.commit()
        return run.id


async def resolve_trace(**_: object) -> str:
    """为本地容量夹具返回稳定 trace，避免测试路径依赖额外的持久化查询。"""

    return "trace-a"


async def seed_local_high_water(
    *,
    storage: SQLAlchemyStorage,
    event_path: Path,
    run_id: str,
    highest_seq: int,
) -> None:
    """构造稀疏历史事件；local 账本必须延续迁移允许的最大 seq。"""

    seed = CanonicalEvent(
        event_id="local-capacity-seed",
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        event_type=CanonicalEventType.RUN_STARTED,
        seq=highest_seq,
        trace_id="trace-a",
    )
    register_local_state_file(event_path, kind="events")
    event_path.write_bytes(canonical_event_bytes(seed) + b"\n")
    async with storage.uow() as uow:
        await uow.session.execute(
            update(RunEventCapacityModel)
            .where(RunEventCapacityModel.run_id == run_id)
            .values(highest_persisted_seq=highest_seq)
        )
        await uow.commit()


def event_bus(*, storage: SQLAlchemyStorage, event_path: Path) -> EventBus:
    """返回共享 trace 与容量账本的 local EventBus。"""

    return EventBus(
        sink=LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace),
        run_trace_resolver=resolve_trace,
        capacity_storage=storage,
    )


__all__ = ["event_bus", "resolve_trace", "seed_local_high_water", "seed_run"]
