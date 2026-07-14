"""PostgreSQL CanonicalEvent event-id 重放隔离契约测试。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from agent_harness.events import CanonicalEvent, CanonicalEventType, PostgreSQLEventSink
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL event-id 重放隔离合同由 service 环境注入 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_event_sink_replay_never_crosses_event_boundaries() -> None:
    """direct sink 重放只能返回同租户、run、trace、scope 与可见性的原事件。"""

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    tenant_id = f"event-replay-{uuid4()}"
    other_tenant_id = f"event-replay-other-{uuid4()}"
    trace_id = f"trace-{tenant_id}"
    other_trace_id = f"trace-{other_tenant_id}"
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_id)
            await uow.tenants.ensure(other_tenant_id)
            session = await uow.sessions.create(
                SessionCreate(tenant_id=tenant_id, user_id="user", agent_id="agent")
            )
            other_session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=other_tenant_id,
                    user_id="other-user",
                    agent_id="other-agent",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    agent_id="agent",
                    trace_id=trace_id,
                )
            )
            other_run_same_tenant = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    agent_id="agent",
                    trace_id=f"trace-other-{tenant_id}",
                )
            )
            other_run = await uow.runs.create(
                RunCreate(
                    tenant_id=other_tenant_id,
                    session_id=other_session.id,
                    agent_id="other-agent",
                    trace_id=other_trace_id,
                )
            )
            await uow.commit()

        sink = PostgreSQLEventSink(storage)
        event_id = f"event-replay:{run.id}"
        event = CanonicalEvent(
            event_id=event_id,
            tenant_id=tenant_id,
            run_id=run.id,
            event_type=CanonicalEventType.RUN_STARTED,
            seq=0,
            trace_id=trace_id,
        )
        persisted = await sink.write(event)

        # 完全相同的串行或并发重放必须收敛到同一条 evidence。
        assert await sink.write(persisted) == persisted
        concurrent = await asyncio.gather(*(sink.write(persisted) for _ in range(4)))
        assert concurrent == [persisted] * 4

        invalid_replays = [
            event.model_copy(
                update={
                    "tenant_id": other_tenant_id,
                    "run_id": other_run.id,
                    "trace_id": other_trace_id,
                }
            ),
            event.model_copy(
                update={
                    "run_id": other_run_same_tenant.id,
                    "trace_id": other_run_same_tenant.trace_id,
                }
            ),
            event.model_copy(update={"trace_id": f"wrong-{trace_id}"}),
            event.model_copy(update={"record_scope": "non_run"}),
            event.model_copy(update={"terminal": True, "visibility": "public"}),
            event.model_copy(update={"visibility": "public"}),
            event.model_copy(update={"event_type": CanonicalEventType.TOOL_CALL_COMPLETED}),
            event.model_copy(update={"event_version": "2.0"}),
            event.model_copy(update={"payload": {"operation": "different"}}),
            event.model_copy(update={"request_id": "different-request"}),
            event.model_copy(update={"span_id": "different-span"}),
        ]
        for replay in invalid_replays:
            with pytest.raises(
                ValueError,
                match="event replay envelope does not match persisted event",
            ) as error:
                await sink.write(replay)
            assert tenant_id not in str(error.value)
            assert run.id not in str(error.value)

        terminal_id = f"event-terminal-replay:{run.id}"
        terminal = await sink.write(
            CanonicalEvent(
                event_id=terminal_id,
                tenant_id=tenant_id,
                run_id=run.id,
                event_type=CanonicalEventType.RUN_COMPLETED,
                seq=0,
                terminal=True,
                visibility="public",
                trace_id=trace_id,
            )
        )
        assert await sink.write(terminal) == terminal
        with pytest.raises(ValueError, match="terminal run events must be public"):
            await sink.write(terminal.model_copy(update={"visibility": "internal"}))

        # 所有拒绝路径都不得为碰撞方创建记录，也不得返回其他边界的事件。
        assert [item.event_id for item in await sink.read(run_id=run.id)] == [
            event_id,
            terminal_id,
        ]
        assert await sink.read(run_id=other_run_same_tenant.id) == []
        assert await sink.read(run_id=other_run.id) == []
    finally:
        await storage.dispose()
