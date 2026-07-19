"""Usage 事件必须绑定 durable outbox 才能消费容量预约。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, PostgreSQLEventSink
from agent_harness.models import UsageEvidenceContext, model_usage_evidence
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


def _forged_started(*, tenant_id: str, run_id: str, trace_id: str) -> CanonicalEvent:
    """构造未关联 durable outbox 的伪造 started 事件，验证 event sink 不会信任调用方自报关联。"""

    return CanonicalEvent(
        event_id=f"usage:{tenant_id}:forged:started",
        tenant_id=tenant_id,
        run_id=run_id,
        agent_id="agent-a",
        event_type=CanonicalEventType.MODEL_REQUEST_STARTED,
        seq=0,
        payload={
            "correlation": {"usage_call_id": "forged"},
            "usage": {"usage_kind": "model", "provider": "fake", "model": "fake-basic"},
        },
        trace_id=trace_id,
    )


@pytest.mark.asyncio
async def test_local_forged_usage_event_cannot_consume_unbound_reservation(
    tmp_path: Path,
) -> None:
    """本地 JSONL 路径拒绝伪造 usage 事件，未绑定预约与磁盘事件文件必须保持原样。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'forged-local.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "forged-local.jsonl"
    try:
        run_id = await seed_run(storage)
        async with storage.uow() as uow:
            await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            before = await uow.event_capacity.snapshot(run_id)
            await uow.commit()

        with pytest.raises(LookupError, match="usage settlement not found"):
            await event_bus(storage=storage, event_path=event_path).publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.MODEL_REQUEST_STARTED,
                payload=_forged_started(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    trace_id="trace-a",
                ).payload,
                trace_id="trace-a",
                event_id="usage:tenant-a:forged:started",
            )

        async with storage.uow() as uow:
            after = await uow.event_capacity.snapshot(run_id)
        assert after == before
        assert not event_path.exists()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_usage_final_cannot_settle_capacity_before_result_is_persisted(
    tmp_path: Path,
) -> None:
    """最终用量事件必须在结果已持久化后才可结算预约，防止事件先行造成不可恢复的账本缺口。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'premature-final.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "premature-final.jsonl"
    usage_call_id = "premature-final"
    try:
        run_id = await seed_run(storage)
        evidence = model_usage_evidence(
            provider="fake",
            model="fake-basic",
            token_usage={"input_tokens": 1, "output_tokens": 1},
            latency_ms=1,
            decision={"provider_called": True},
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
                event_id=f"usage:tenant-a:{usage_call_id}:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            await uow.commit()
        bus = event_bus(storage=storage, event_path=event_path)
        await bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.MODEL_REQUEST_STARTED,
            payload={
                "correlation": {"usage_call_id": usage_call_id},
                "usage": {
                    "usage_kind": "model",
                    "provider": "fake",
                    "model": "fake-basic",
                    "decision": {"provider_called": True},
                },
            },
            trace_id="trace-a",
            event_id=f"usage:tenant-a:{usage_call_id}:started",
        )
        async with storage.uow() as uow:
            before = await uow.event_capacity.snapshot(run_id)

        with pytest.raises(RuntimeError, match="persisted result"):
            await bus.publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
                payload={
                    "correlation": {"usage_call_id": usage_call_id},
                    "usage": evidence.to_payload(),
                    "outcome": "completed",
                },
                trace_id="trace-a",
                event_id=f"usage:tenant-a:{usage_call_id}:final",
            )

        async with storage.uow() as uow:
            after = await uow.event_capacity.snapshot(run_id)
        assert after == before
        assert before.highest_persisted_seq == 1
        assert before.outstanding_reserved_event_count == 1
    finally:
        await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL usage binding 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_forged_usage_event_cannot_consume_unbound_reservation() -> None:
    """PostgreSQL sink 同样按 outbox 绑定校验伪造事件，跨存储后端不能出现容量绕过。"""

    async with isolated_database("usage_binding") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            async with storage.uow() as uow:
                await uow.tenants.ensure("tenant-pg")
                session = await uow.sessions.create(
                    SessionCreate(
                        tenant_id="tenant-pg",
                        user_id="user-a",
                        agent_id="agent-a",
                    )
                )
                run = await uow.runs.create(
                    RunCreate(
                        tenant_id="tenant-pg",
                        session_id=session.id,
                        agent_id="agent-a",
                        trace_id="trace-pg",
                    )
                )
                await uow.event_capacity.reserve(
                    run_id=run.id,
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                )
                before = await uow.event_capacity.snapshot(run.id)
                await uow.commit()

            with pytest.raises(LookupError, match="usage settlement not found"):
                await PostgreSQLEventSink(storage).write(
                    _forged_started(
                        tenant_id="tenant-pg",
                        run_id=run.id,
                        trace_id="trace-pg",
                    )
                )
            async with storage.uow() as uow:
                after = await uow.event_capacity.snapshot(run.id)
            assert after == before
        finally:
            await storage.dispose()
