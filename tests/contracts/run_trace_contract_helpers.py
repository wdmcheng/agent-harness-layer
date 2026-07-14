"""Run-scoped evidence 合同共享的最小持久化 run fixture。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


def sqlite_dsn(path: Path) -> str:
    """为隔离合同测试构造 async SQLite DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def persisted_event_bus(
    storage: SQLAlchemyStorage,
    sink: LocalJsonlEventSink,
    *,
    artifact_store: FileArtifactStore | None = None,
) -> EventBus:
    """复用生产持久化 run-trace 门禁，不注入内存假 resolver。"""

    return EventBus(
        sink=sink,
        artifact_store=artifact_store,
        run_trace_resolver=StorageRunTraceResolver(storage),
    )


async def seed_persisted_run(
    storage: SQLAlchemyStorage,
    *,
    trace_id: str,
    agent_id: str = "examples.basic",
    tenant_id: str = "default",
) -> str:
    """通过公开 UoW seam 创建带 binding/context 的 root run。"""

    session_id = f"trace-fixture-{uuid4()}"
    async with storage.uow() as uow:
        await uow.tenants.ensure(tenant_id)
        await uow.sessions.create(
            SessionCreate(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id="trace-fixture",
                agent_id=agent_id,
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id=tenant_id,
                session_id=session_id,
                agent_id=agent_id,
                trace_id=trace_id,
            ),
            execution_context={"trace_id": trace_id},
        )
        await uow.commit()
    return run.id
