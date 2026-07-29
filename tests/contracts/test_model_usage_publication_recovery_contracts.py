"""模型 usage 最终证据写入失败后的恢复合同。"""

from pathlib import Path

import pytest
from tests.contracts.model_usage_recovery_test_support import usage_run as _usage_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


@pytest.mark.asyncio
async def test_model_final_publish_recovery_does_not_replay_provider(tmp_path: Path) -> None:
    """验证最终 usage 事件写入失败后，恢复只补事件且绝不第二次调用 provider。"""

    class SpyProvider(FakeModelProvider):
        """统计 provider 调用次数的假实现，用于证明恢复没有重复外部副作用。"""

        calls = 0

        async def complete(self, request: ModelRequest, *, plan: object):
            """记录一次真实调用后委托 fake provider，保持原始响应形状。"""

            self.calls += 1
            return await super().complete(request, plan=plan)

    class FailFinalOnceSink:
        """仅在第一条 usage 最终事件写入前失败的 sink 包装器。"""

        manages_event_capacity = False

        def __init__(self, delegate: LocalJsonlEventSink) -> None:
            """保存耐久 JSONL delegate 并初始化一次性故障开关。"""

            self.delegate = delegate
            self.failed = False

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            """对首次 usage 更新事件抛出写前失败，其余事件委托耐久 sink。"""

            if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self.failed:
                self.failed = True
                raise OSError("injected final write failure")
            return await self.delegate.write(event)

        async def read(self, *, run_id: str, after_seq: int = 0):
            """透传事件读取，供恢复后验证稳定事件顺序。"""

            return await self.delegate.read(run_id=run_id, after_seq=after_seq)

        async def latest_seq(self, run_id: str) -> int:
            """透传最后序号读取，保持 EventBus 所需查询能力。"""

            return await self.delegate.latest_seq(run_id)

        async def has_terminal(self, run_id: str) -> bool:
            """透传终结查询，保持 sink 协议完整。"""

            return await self.delegate.has_terminal(run_id)

    database = tmp_path / "recovery.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        """为使用 durable sink 的测试返回固定 trace。"""

        return "trace-a"

    durable_sink = LocalJsonlEventSink(
        tmp_path / "recovery-events.jsonl",
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
                sink=FailFinalOnceSink(durable_sink),
                run_trace_resolver=resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="injected final write failure"):
            await failing.complete(
                ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-recover",
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
        assert events[-1].event_id == "usage:tenant-a:usage-recover:final"
    finally:
        await storage.dispose()
