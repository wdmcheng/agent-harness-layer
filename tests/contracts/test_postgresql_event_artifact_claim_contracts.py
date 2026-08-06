"""PostgreSQL event claim、artifact 与 fan-out 原子性契约测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, PostgreSQLEventSink
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.models import AgentRunModel
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


class RecordingArtifactStore(FileArtifactStore):
    """区分获胜 claim 与被拒 collision 是否真正 materialize。"""

    def __init__(self, root: Path) -> None:
        """初始化真实文件制品存储和 materialize 计数，便于区分 replay 与首次获胜写入。"""

        super().__init__(root)
        self.calls = 0

    def write_json(self, payload: dict[str, Any]) -> Any:
        """记录真实落盘次数后委托父类，证明 event-id 竞争失败者不会提前创建制品。"""

        self.calls += 1
        return super().write_json(payload)


class FailingArtifactStore(FileArtifactStore):
    """PG 事务内 artifact 失败夹具。"""

    def write_json(self, payload: dict[str, Any]) -> Any:
        """在任何文件写入前注入故障，验证数据库 claim 不会在 artifact 未 materialize 时提交。"""

        raise OSError("simulated postgres artifact failure")


class ArtifactPublishArgs(TypedDict):
    """跨 bus 并发发布大载荷时保持一致的受保护事件字段，便于构造同 ID 冲突。"""

    tenant_id: str
    run_id: str
    event_type: CanonicalEventType
    event_id: str
    trace_id: str
    payload: dict[str, Any]


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL event-id claim 与 artifact 顺序合同由 service 环境注入 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_event_claim_is_atomic_before_artifact_and_fanout(
    tmp_path: Path,
) -> None:
    """真实 PG 覆盖 IntegrityError retry、跨边界 artifact race 与失败回滚。"""

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    tenant_a = f"event-claim-a-{uuid4()}"
    tenant_b = f"event-claim-b-{uuid4()}"
    trace_a = f"trace-{tenant_a}"
    trace_b = f"trace-{tenant_b}"
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_a)
            await uow.tenants.ensure(tenant_b)
            session_a = await uow.sessions.create(
                SessionCreate(tenant_id=tenant_a, user_id="user-a", agent_id="agent-a")
            )
            session_b = await uow.sessions.create(
                SessionCreate(tenant_id=tenant_b, user_id="user-b", agent_id="agent-b")
            )
            run_a = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant_a,
                    session_id=session_a.id,
                    agent_id="agent-a",
                    trace_id=trace_a,
                )
            )
            run_b = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant_b,
                    session_id=session_b.id,
                    agent_id="agent-b",
                    trace_id=trace_b,
                )
            )
            await uow.commit()

        # 先锁 run row，让两个 writer 都完成 event-id fast-path miss 后阻塞在
        # 同一 seq mutex；释放后 loser 必经 IntegrityError fallback 并校验 replay。
        blocker = await storage.engine.connect()
        transaction = await blocker.begin()
        await blocker.execute(
            select(AgentRunModel.id).where(AgentRunModel.id == run_a.id).with_for_update()
        )
        direct = CanonicalEvent(
            event_id=f"forced-integrity-{run_a.id}",
            tenant_id=tenant_a,
            run_id=run_a.id,
            event_type=CanonicalEventType.RUN_STARTED,
            seq=0,
            trace_id=trace_a,
        )
        direct_tasks = [
            asyncio.create_task(PostgreSQLEventSink(storage).write(direct)) for _ in range(2)
        ]
        await asyncio.sleep(0.1)
        assert not any(task.done() for task in direct_tasks)
        await transaction.commit()
        await blocker.close()
        direct_results = await asyncio.gather(*direct_tasks)
        assert direct_results[0] == direct_results[1]
        persisted_direct = await PostgreSQLEventSink(storage).read(run_id=run_a.id)
        assert [event.event_id for event in persisted_direct] == [direct.event_id]

        resolver = StorageRunTraceResolver(storage)
        boundary_bus = EventBus(
            sink=PostgreSQLEventSink(storage),
            run_trace_resolver=resolver,
        )

        async def publish_boundary(**updates: Any) -> CanonicalEvent:
            """以固定 event-id 发布可控边界信封，验证重复调用只能接受完全相同的持久化语义。"""

            values: dict[str, Any] = {
                "tenant_id": tenant_a,
                "run_id": run_a.id,
                "trace_id": trace_a,
                "record_scope": "non_run",
                "terminal": False,
                "visibility": "internal",
            }
            values.update(updates)
            return await boundary_bus.publish(
                tenant_id=values["tenant_id"],
                run_id=values["run_id"],
                event_type=CanonicalEventType.RUN_STARTED,
                event_id=f"bus-boundary-{run_a.id}",
                trace_id=values["trace_id"],
                record_scope=values["record_scope"],
                terminal=values["terminal"],
                visibility=values["visibility"],
            )

        boundary = await publish_boundary()
        assert await publish_boundary() == boundary
        for updates in (
            {"tenant_id": tenant_b},
            {"run_id": run_b.id},
            {"trace_id": trace_b},
            {"record_scope": "run"},
            {"visibility": "public"},
        ):
            with pytest.raises(
                ValueError,
                match="event replay envelope does not match persisted event",
            ) as error:
                await publish_boundary(**updates)
            assert str(error.value) == "event replay envelope does not match persisted event"

        artifact_root = tmp_path / "artifacts"
        store_a = RecordingArtifactStore(artifact_root)
        store_b = RecordingArtifactStore(artifact_root)
        bus_a = EventBus(
            sink=PostgreSQLEventSink(storage),
            artifact_store=store_a,
            inline_payload_bytes=32,
            run_trace_resolver=resolver,
        )
        bus_b = EventBus(
            sink=PostgreSQLEventSink(storage),
            artifact_store=store_b,
            inline_payload_bytes=32,
            run_trace_resolver=resolver,
        )
        event_id = f"artifact-race-{uuid4()}"
        kwargs_a: ArtifactPublishArgs = {
            "tenant_id": tenant_a,
            "run_id": run_a.id,
            "event_type": CanonicalEventType.ARTIFACT_CREATED,
            "event_id": event_id,
            "trace_id": trace_a,
            "payload": {"text": "private-a" * 64},
        }
        kwargs_b: ArtifactPublishArgs = {
            "tenant_id": tenant_b,
            "run_id": run_b.id,
            "event_type": CanonicalEventType.ARTIFACT_CREATED,
            "event_id": event_id,
            "trace_id": trace_b,
            "payload": {"text": "private-b" * 64},
        }
        fanout: list[str] = []

        async def publish_and_fanout(
            bus: EventBus,
            kwargs: ArtifactPublishArgs,
        ) -> CanonicalEvent:
            """发布成功后才记录 fan-out，确保竞争失败者既无制品副作用也不会触发下游通知。"""

            published = await bus.publish(**kwargs)
            fanout.append(published.run_id)
            return published

        race = await asyncio.gather(
            publish_and_fanout(bus_a, kwargs_a),
            publish_and_fanout(bus_b, kwargs_b),
            return_exceptions=True,
        )
        winners = [item for item in race if isinstance(item, CanonicalEvent)]
        conflicts = [item for item in race if isinstance(item, ValueError)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        assert str(conflicts[0]) == "event replay envelope does not match persisted event"
        assert "private-a" not in str(conflicts[0])
        assert "private-b" not in str(conflicts[0])
        assert store_a.calls + store_b.calls == 1
        assert len(list(artifact_root.glob("*.json"))) == 1
        assert fanout == [winners[0].run_id]

        if winners[0].run_id == run_a.id:
            retry = await bus_a.publish(**kwargs_a)
        else:
            retry = await bus_b.publish(**kwargs_b)
        assert retry == winners[0]
        assert store_a.calls + store_b.calls == 1

        winner_bus = bus_a if winners[0].run_id == run_a.id else bus_b
        winner_kwargs = kwargs_a if winners[0].run_id == run_a.id else kwargs_b
        semantic_conflict_kwargs: ArtifactPublishArgs = {
            **winner_kwargs,
            "payload": {"text": "different" * 64},
        }
        with pytest.raises(
            ValueError,
            match="event replay envelope does not match persisted event",
        ):
            await winner_bus.publish(**semantic_conflict_kwargs)
        assert store_a.calls + store_b.calls == 1
        assert len(list(artifact_root.glob("*.json"))) == 1

        failing_bus = EventBus(
            sink=PostgreSQLEventSink(storage),
            artifact_store=FailingArtifactStore(tmp_path / "failing-artifacts"),
            inline_payload_bytes=32,
            run_trace_resolver=resolver,
        )
        failed_id = f"artifact-failure-{uuid4()}"
        with pytest.raises(OSError, match="simulated postgres artifact failure"):
            await failing_bus.publish(
                tenant_id=tenant_a,
                run_id=run_a.id,
                event_type=CanonicalEventType.ARTIFACT_CREATED,
                event_id=failed_id,
                trace_id=trace_a,
                payload={"text": "failure" * 64},
            )
        assert failed_id not in {
            event.event_id for event in await PostgreSQLEventSink(storage).read(run_id=run_a.id)
        }
        assert not (tmp_path / "failing-artifacts").exists()
    finally:
        await storage.dispose()
