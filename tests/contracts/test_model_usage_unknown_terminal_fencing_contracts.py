"""未知模型 usage 对公开 terminal 的容量栅栏合同。"""

from pathlib import Path

import pytest
from tests.contracts.model_usage_recovery_test_support import usage_run as _usage_run

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import UsageEvidenceContext, model_usage_evidence
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


@pytest.mark.parametrize(
    "terminal_type",
    [
        CanonicalEventType.RUN_COMPLETED,
        CanonicalEventType.RUN_FAILED,
        CanonicalEventType.RUN_CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_unknown_usage_result_blocks_every_public_terminal(
    tmp_path: Path,
    terminal_type: CanonicalEventType,
) -> None:
    """验证 usage 结果未知时，完成、失败和取消三种公开终结事件都会被容量栅栏阻断。"""

    database = tmp_path / f"pending-{terminal_type.value}.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / f"pending-{terminal_type.value}.jsonl")

    async def resolve_trace(**_: object) -> str:
        """为每个终结类型参数化场景提供固定 trace。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        async with storage.uow() as uow:
            reserved = await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            await uow.evidence_outbox.start_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id="usage-unknown",
                event_id="usage:tenant-a:usage-unknown:final",
                reserved_event_count=reserved,
                started_evidence=model_usage_evidence(
                    provider="fake",
                    model="fake-basic",
                    token_usage={},
                    latency_ms=0,
                    decision={"provider_called": False},
                    context=UsageEvidenceContext(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        agent_id="agent-a",
                        trace_id="trace-a",
                    ),
                ).to_payload(),
            )
            await uow.commit()

        bus = EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        with pytest.raises(RuntimeError, match="pending evidence blocks terminal"):
            await bus.publish(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                event_type=terminal_type,
                trace_id="trace-a",
                terminal=True,
                visibility="public",
            )
        assert await sink.read(run_id=run_id) == []
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(run_id)
            assert capacity.outstanding_reserved_event_count == 2
            assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()
