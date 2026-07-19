"""Model usage 在 local JSONL/SQLite 提交窗口硬退出后的恢复合同。"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRouter,
    ModelRouterConfig,
    ModelUsageEvidence,
    UsageEvidenceContext,
    model_usage_evidence,
)
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


def _publish_final_then_exit_before_capacity_commit(
    dsn: str,
    event_path: str,
    evidence_payload: dict[str, object],
    usage_call_id: str,
) -> None:
    """子进程在 JSONL fsync 后绕过异常补偿，模拟 SIGKILL/掉电窗口。"""

    async def publish() -> None:
        """建立真实 outbox/capacity 组合，并在 final 发布的数据库提交点触发硬退出。"""

        async def hard_exit_commit(_uow: SQLAlchemyUnitOfWork) -> None:
            """用不可捕获的进程退出模拟提交前掉电，禁止正常异常处理收口现场。"""

            os._exit(23)

        SQLAlchemyUnitOfWork.commit = hard_exit_commit  # type: ignore[method-assign]
        storage = SQLAlchemyStorage.from_dsn(dsn)
        bus = EventBus(
            sink=LocalJsonlEventSink(
                Path(event_path),
                run_trace_resolver=resolve_trace,
            ),
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        await UsageEvidenceLifecycle(
            event_bus=bus,
            evidence=ModelUsageEvidence.model_validate(evidence_payload),
            usage_call_id=usage_call_id,
        ).publish_final()

    asyncio.run(publish())


@pytest.mark.asyncio
async def test_local_hard_exit_replay_repairs_capacity_before_outbox_publish(
    tmp_path: Path,
) -> None:
    """append-ahead 重放必须先幂等结算 capacity，再公开 outbox/terminal。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'hard-exit-capacity.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "hard-exit-capacity.jsonl"
    usage_call_id = "usage-hard-exit"
    run_id = await seed_run(storage)
    evidence = model_usage_evidence(
        provider="fake",
        model="fake-basic",
        token_usage={"input_tokens": 2, "output_tokens": 1},
        latency_ms=1,
        decision={"provider_called": True},
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            trace_id="trace-a",
        ),
    )
    try:
        async with storage.uow() as uow:
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=usage_call_id,
                event_id=f"usage:tenant-a:{usage_call_id}:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            await uow.evidence_outbox.persist_result(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
                result={"evidence": evidence.to_payload(), "outcome": "completed"},
            )
            await uow.commit()
        bus = EventBus(
            sink=LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace),
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        await UsageEvidenceLifecycle(
            event_bus=bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        ).publish_started()
        assert claim.created is True
    finally:
        await storage.dispose()

    process = multiprocessing.get_context("spawn").Process(
        target=_publish_final_then_exit_before_capacity_commit,
        args=(dsn, str(event_path), evidence.to_payload(), usage_call_id),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 23

    recovered_storage = SQLAlchemyStorage.from_dsn(dsn)
    recovered_sink = LocalJsonlEventSink(event_path, run_trace_resolver=resolve_trace)
    recovered_bus = EventBus(
        sink=recovered_sink,
        run_trace_resolver=resolve_trace,
        capacity_storage=recovered_storage,
    )
    try:
        events_before = await recovered_sink.read(run_id=run_id)
        async with recovered_storage.uow() as uow:
            capacity_before = await uow.event_capacity.snapshot(run_id)
        assert [event.seq for event in events_before] == [1, 2]
        assert capacity_before.highest_persisted_seq == 1
        assert capacity_before.outstanding_reserved_event_count == 1

        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": FakeModelProvider()},
            ),
            storage=recovered_storage,
            event_bus=recovered_bus,
        )
        assert await service.recover_pending(run_id=run_id) == 1
        async with recovered_storage.uow() as uow:
            assert await uow.evidence_outbox.pending(run_id=run_id) == []
            capacity_after = await uow.event_capacity.snapshot(run_id)
        assert capacity_after.highest_persisted_seq == 2
        assert capacity_after.outstanding_reserved_event_count == 0
        assert capacity_after.terminal_reservation == 1

        replayed = await UsageEvidenceLifecycle(
            event_bus=recovered_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        ).publish_final()
        async with recovered_storage.uow() as uow:
            replayed_capacity = await uow.event_capacity.snapshot(run_id)
        assert replayed.seq == 2
        assert replayed_capacity == capacity_after

        terminal = await recovered_bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.RUN_COMPLETED,
            terminal=True,
            visibility="public",
            trace_id="trace-a",
            event_id=f"run-terminal:{run_id}",
        )
        async with recovered_storage.uow() as uow:
            final_capacity = await uow.event_capacity.snapshot(run_id)
        assert terminal.seq == 3
        assert final_capacity.highest_persisted_seq == 3
        assert final_capacity.outstanding_reserved_event_count == 0
        assert final_capacity.terminal_reservation == 0
    finally:
        await recovered_storage.dispose()
