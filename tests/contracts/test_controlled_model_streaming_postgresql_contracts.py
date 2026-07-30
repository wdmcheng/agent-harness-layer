"""受控模型文本流的真实 PostgreSQL claim、锁、payload 与容量结算合同。"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

import pytest
from sqlalchemy import delete
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.model_usage_capacity_test_helpers import seed_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, PostgreSQLEventSink
from agent_harness.models import ModelUsageEvidence, UsageEvidenceLifecycle
from agent_harness.storage import (
    RunCreate,
    SessionCreate,
    SQLAlchemyStorage,
    run_migrations,
)
from agent_harness.storage.evidence_repositories import EvidenceOperationKind
from agent_harness.storage.models import RunEvidenceOutboxModel
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver
from agent_harness.storage.stream_evidence_repositories import (
    stream_completed_event_id,
    stream_delta_event_id,
    stream_group_id,
    stream_usage_event_id,
)


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL stream 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_concurrent_stream_claim_and_sink_binding_are_atomic() -> None:
    """两个 claimant 只有一个预约 65 槽；sink 篡改失败不消耗，正确事件原子前进。"""

    async with isolated_database("model_stream") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        usage_call_id = "3" * 64
        try:
            run_id = await seed_run(storage)

            async def claim():  # type: ignore[no-untyped-def]
                async with storage.uow() as uow:
                    result = await uow.evidence_outbox.claim_stream(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        usage_call_id=usage_call_id,
                    )
                    await uow.commit()
                    return result.created

            created = await asyncio.gather(claim(), claim())
            assert sorted(created) == [False, True]

            intent = CanonicalEvent(
                event_id=stream_delta_event_id(usage_call_id, 1),
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
                seq=0,
                payload={
                    "correlation": {"usage_call_id": usage_call_id},
                    "attempt": 1,
                    "chunk_ordinal": 1,
                    "text": "postgres",
                },
                visibility="public",
                trace_id="trace-a",
            )
            async with storage.uow() as uow:
                await uow.evidence_outbox.persist_stream_event(intent)
                before = await uow.event_capacity.snapshot(run_id)
                await uow.commit()

            with pytest.raises(ValueError, match="durable stream intent"):
                await PostgreSQLEventSink(storage).write(
                    intent.model_copy(
                        update={
                            "payload": {
                                **cast(dict[str, Any], intent.payload),
                                "text": "tampered",
                            }
                        }
                    )
                )
            persisted = await PostgreSQLEventSink(storage).write(intent)
            async with storage.uow() as uow:
                after = await uow.event_capacity.snapshot(run_id)

            assert persisted.seq == 1
            assert before.outstanding_reserved_event_count == 65
            assert after.highest_persisted_seq == 1
            assert after.outstanding_reserved_event_count == 64
        finally:
            await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL stream 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_stream_sink_rejects_missing_predecessor_row() -> None:
    """真实 PostgreSQL sink 必须把缺失 ordinal 视为损坏，不能把空查询当作已结算。"""

    async with isolated_database("model_stream_missing_predecessor") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        usage_call_id = "4" * 64
        try:
            run_id = await seed_run(storage)
            second = CanonicalEvent(
                event_id=stream_delta_event_id(usage_call_id, 2),
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
                seq=0,
                payload={
                    "correlation": {"usage_call_id": usage_call_id},
                    "attempt": 1,
                    "chunk_ordinal": 2,
                    "text": "不能越序",
                },
                visibility="public",
                trace_id="trace-a",
            )
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_stream(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                )
                await uow.evidence_outbox.persist_stream_event(second)
                await uow.session.execute(
                    delete(RunEvidenceOutboxModel).where(
                        RunEvidenceOutboxModel.event_id == stream_delta_event_id(usage_call_id, 1)
                    )
                )
                await uow.commit()

            with pytest.raises(LookupError, match="predecessor"):
                await PostgreSQLEventSink(storage).write(second)
        finally:
            await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL stream 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_migration_head_persists_max_tenant_and_all_stream_identities() -> None:
    """现有列在最大 tenant 与 ordinal 64 下容纳五类定长 identity，无隐式迁移。"""

    async with isolated_database("model_stream_identity") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        tenant_id = "t" * 64
        usage_call_id = "f" * 64
        try:
            async with storage.uow() as uow:
                await uow.tenants.ensure(tenant_id)
                await uow.sessions.ensure(
                    SessionCreate(
                        session_id="stream-identity-session",
                        tenant_id=tenant_id,
                        user_id="stream-identity-user",
                        agent_id="agent-a",
                    )
                )
                run = await uow.runs.create(
                    RunCreate(
                        tenant_id=tenant_id,
                        session_id="stream-identity-session",
                        agent_id="agent-a",
                        trace_id="trace-stream-identity",
                    )
                )
                run_id = run.id
                await uow.commit()
            started = ModelUsageEvidence(
                usage_kind="model",
                tenant_id=tenant_id,
                provider="fake",
                model="fake-basic",
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=0,
                decision={"usage_event_identity": {"ref": "stream-usage", "version": "v1"}},
                run_id=run_id,
                agent_id="agent-a",
                request_id=None,
                trace_id="trace-stream-identity",
            )
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_usage(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                    event_id=stream_usage_event_id(usage_call_id, "final"),
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    started_evidence=started.to_payload(),
                )
                await uow.evidence_outbox.claim_stream(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                )
                await uow.commit()

            sink = PostgreSQLEventSink(storage)
            published_started = await UsageEvidenceLifecycle(
                event_bus=EventBus(
                    sink=sink,
                    capacity_storage=storage,
                    run_trace_resolver=StorageRunTraceResolver(storage),
                ),
                evidence=started,
                usage_call_id=usage_call_id,
            ).publish_started()
            async with storage.uow() as uow:
                group = await uow.evidence_outbox.ordered_group(
                    group_id=stream_group_id(usage_call_id)
                )
                usage = await uow.evidence_outbox.get_usage(
                    tenant_id=tenant_id,
                    usage_call_id=usage_call_id,
                )
                persisted = (
                    group[0].group_id,
                    group[63].event_id,
                    group[64].event_id,
                    usage.event_id,
                )

            identities = (
                stream_group_id(usage_call_id),
                stream_delta_event_id(usage_call_id, 64),
                stream_completed_event_id(usage_call_id),
                stream_usage_event_id(usage_call_id, "started"),
                stream_usage_event_id(usage_call_id, "final"),
            )
            assert persisted == (identities[0], identities[1], identities[2], identities[4])
            assert published_started.event_id == identities[3]
            assert [len(item) for item in identities] == [77, 82, 79, 79, 79]
        finally:
            await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL stream 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_stream_claim_rollback_cancel_and_terminal_fence_are_atomic() -> None:
    """claim 回滚不留部分组；已提交占位阻止 terminal，取消后只释放 outstanding。"""

    async with isolated_database("model_stream_rollback") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        usage_call_id = "a" * 64
        try:
            run_id = await seed_run(storage)
            with pytest.raises(RuntimeError, match="rollback sentinel"):
                async with storage.uow() as uow:
                    await uow.evidence_outbox.claim_stream(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        usage_call_id=usage_call_id,
                    )
                    raise RuntimeError("rollback sentinel")
            async with storage.uow() as uow:
                assert (
                    await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
                    == []
                )
                after_rollback = await uow.event_capacity.snapshot(run_id)
            assert after_rollback.outstanding_reserved_event_count == 0

            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_stream(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                )
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(RuntimeError, match="pending evidence blocks terminal"):
                    await uow.event_capacity.assert_terminal_publishable(run_id=run_id)
                released = await uow.evidence_outbox.cancel_unused_stream(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                    used_delta_count=0,
                    keep_completed=False,
                )
                await uow.event_capacity.assert_terminal_publishable(run_id=run_id)
                await uow.commit()
            async with storage.uow() as uow:
                group = await uow.evidence_outbox.ordered_group(
                    group_id=stream_group_id(usage_call_id)
                )
                capacity = await uow.event_capacity.snapshot(run_id)
                states = [item.state for item in group]
            assert released == 65
            assert states == ["cancelled"] * 65
            assert capacity.highest_persisted_seq == 0
            assert capacity.outstanding_reserved_event_count == 0
        finally:
            await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL stream 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_legacy_usage_recovery_keeps_original_event_identity() -> None:
    """无 stream marker 的 durable usage 仍按旧 tenant identity 补投，不重键历史行。"""

    async with isolated_database("model_stream_legacy") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        usage_call_id = "b" * 64
        try:
            run_id = await seed_run(storage)
            started = ModelUsageEvidence(
                usage_kind="model",
                tenant_id="tenant-a",
                provider="fake",
                model="fake-basic",
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=0,
                decision={},
                run_id=run_id,
                agent_id="agent-a",
                request_id=None,
                trace_id="trace-a",
            )
            legacy_final_id = f"usage:tenant-a:{usage_call_id}:final"
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_usage(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                    event_id=legacy_final_id,
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    started_evidence=started.to_payload(),
                )
                await uow.commit()
            lifecycle = UsageEvidenceLifecycle(
                event_bus=EventBus(
                    sink=PostgreSQLEventSink(storage),
                    capacity_storage=storage,
                    run_trace_resolver=StorageRunTraceResolver(storage),
                ),
                evidence=started,
                usage_call_id=usage_call_id,
            )
            first = await lifecycle.publish_started()
            final = started.model_copy(
                update={
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "latency_ms": 1,
                }
            )
            async with storage.uow() as uow:
                await uow.evidence_outbox.persist_result(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                    result={"evidence": final.to_payload(), "outcome": "completed"},
                )
                pending = await uow.evidence_outbox.pending_usage_run_ids()
                await uow.commit()
            second = await UsageEvidenceLifecycle(
                event_bus=EventBus(
                    sink=PostgreSQLEventSink(storage),
                    capacity_storage=storage,
                    run_trace_resolver=StorageRunTraceResolver(storage),
                ),
                evidence=final,
                usage_call_id=usage_call_id,
            ).publish_final()

            assert pending == [run_id]
            assert first.event_id == f"usage:tenant-a:{usage_call_id}:started"
            assert second.event_id == legacy_final_id
            assert not first.event_id.startswith("usage-stream:")
            assert not second.event_id.startswith("usage-stream:")
        finally:
            await storage.dispose()
