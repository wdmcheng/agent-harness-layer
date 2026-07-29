"""模型 usage 恢复与审批有序 outbox 的隔离合同。"""

from pathlib import Path

import pytest
from tests.contracts.model_usage_recovery_test_support import usage_run as _usage_run

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    UsageEvidenceLifecycle,
    model_usage_evidence,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


@pytest.mark.asyncio
async def test_model_recovery_ignores_ordered_approval_outbox_items(tmp_path: Path) -> None:
    """验证模型恢复只补投模型 usage，不会消费同一 run 中审批的有序 outbox 组。"""

    database = tmp_path / "mixed-recovery.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "mixed-recovery.jsonl")

    async def resolve_trace(**_: object) -> str:
        """为本地 sink 返回稳定 trace，避免恢复测试依赖运行时 trace 查询。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
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
        bus = EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        )
        async with storage.uow() as uow:
            await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id="mixed-model",
                event_id="usage:tenant-a:mixed-model:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            await uow.commit()
        await UsageEvidenceLifecycle(
            event_bus=bus,
            evidence=evidence,
            usage_call_id="mixed-model",
        ).publish_started()
        async with storage.uow() as uow:
            await uow.evidence_outbox.persist_result(
                tenant_id="tenant-a",
                usage_call_id="mixed-model",
                result={"evidence": evidence.to_payload(), "outcome": "completed"},
            )
            await uow.evidence_outbox.stage_ordered_group(
                tenant_id="tenant-a",
                run_id=run_id,
                group_id="approval:mixed:resolution",
                items=[
                    {
                        "event_id": "approval-resolution:mixed",
                        "operation_kind": "approval_resolution",
                        "sequence_in_group": 1,
                        "reserved_event_count": 0,
                        "result": {"status": "approved"},
                    }
                ],
            )
            await uow.commit()

        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": FakeModelProvider()},
            ),
            storage=storage,
            event_bus=bus,
        )
        assert await service.recover_pending(run_id=run_id) == 1
        events = await sink.read(run_id=run_id)
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        async with storage.uow() as uow:
            pending = await uow.evidence_outbox.pending(run_id=run_id)
            pending_kinds = [item.operation_kind for item in pending]
        assert pending_kinds == ["approval_resolution"]
    finally:
        await storage.dispose()
