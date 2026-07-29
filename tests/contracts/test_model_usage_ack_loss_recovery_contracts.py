"""模型 usage 最终证据确认丢失与容量结算恢复合同。"""

from pathlib import Path

import pytest
from tests.contracts.model_usage_recovery_test_support import usage_run as _usage_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


@pytest.mark.asyncio
async def test_model_final_ack_loss_replays_event_only_and_settles_capacity(tmp_path: Path) -> None:
    """验证最终事件已落库但确认丢失时，恢复幂等补投并清空本地容量预留。"""

    class SpyProvider(FakeModelProvider):
        """以实例级计数记录 provider 调用，避免不同测试实例共享状态。"""

        def __init__(self) -> None:
            """初始化调用计数，供恢复前后精确比较。"""

            self.calls = 0

        async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
            """记录调用并委托 fake provider，保持生产服务预期的响应类型。"""

            self.calls += 1
            return await super().complete(request, plan=plan)

    class LoseFinalAckOnceSink:
        """在最终事件已持久化后仅丢失一次确认的 sink 包装器。"""

        manages_event_capacity = False

        def __init__(self, delegate: LocalJsonlEventSink) -> None:
            """保存 durable delegate 并初始化一次性确认丢失开关。"""

            self.delegate = delegate
            self.lost = False

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            """先委托真实写入，再对首条 usage 更新模拟调用方未收到确认。"""

            persisted = await self.delegate.write(event)
            if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self.lost:
                self.lost = True
                raise OSError("injected final acknowledgement loss")
            return persisted

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            """透传读取，供断言事件不会因重放重复出现。"""

            return await self.delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            """透传最后序号查询，满足 EventBus reader 协议。"""

            return await self.delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            """透传终结查询，满足 EventBus reader 协议。"""

            return await self.delegate.has_terminal(run_id)

    database = tmp_path / "ack-loss.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        """为确认丢失场景提供稳定 trace，不引入额外存储读取。"""

        return "trace-a"

    durable_sink = LocalJsonlEventSink(
        tmp_path / "ack-loss-events.jsonl",
        run_trace_resolver=resolve_trace,
    )
    provider = SpyProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    try:
        run_id = await _usage_run(storage)
        failing = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(
                sink=LoseFinalAckOnceSink(durable_sink),
                run_trace_resolver=resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="acknowledgement loss"):
            await failing.complete(
                ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-ack-loss",
            )

        recovering = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=resolve_trace),
        )
        assert await recovering.recover_pending(run_id=run_id) == 1
        assert provider.calls == 1
        events = await durable_sink.read(run_id=run_id)
        assert [item.event_type for item in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert events[-1].event_id == "usage:tenant-a:usage-ack-loss:final"
        async with storage.uow() as uow:
            assert await uow.evidence_outbox.pending(run_id=run_id) == []
            capacity = await uow.event_capacity.snapshot(run_id)
            assert capacity.highest_persisted_seq == 2
            assert capacity.outstanding_reserved_event_count == 0
            assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()
