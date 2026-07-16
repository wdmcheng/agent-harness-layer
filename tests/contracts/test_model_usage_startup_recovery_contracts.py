"""模型用量启动恢复与 provider 防重放合同测试。"""

from __future__ import annotations

from tests.contracts.test_model_usage_idempotency_contracts import (
    CountingModelProvider as CountingModelProvider,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    FailFinalOnceSink as FailFinalOnceSink,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelInvocationService as ModelInvocationService,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelRequest as ModelRequest,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelRouter as ModelRouter,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    ModelRouterConfig as ModelRouterConfig,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    Path as Path,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _assert_settled_once as _assert_settled_once,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _context as _context,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _resolve_trace as _resolve_trace,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    _seed_run as _seed_run,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    pytest as pytest,
)
from tests.contracts.test_model_usage_idempotency_contracts import (
    run_migrations as run_migrations,
)


@pytest.mark.asyncio
async def test_runtime_startup_recovery_republishes_usage_before_executor_replay(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'runtime-recovery.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    durable_sink = LocalJsonlEventSink(
        tmp_path / "runtime-recovery.jsonl",
        run_trace_resolver=_resolve_trace,
    )
    provider = CountingModelProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    try:
        run_id = await _seed_run(storage)
        failing = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(
                sink=FailFinalOnceSink(durable_sink),
                run_trace_resolver=_resolve_trace,
            ),
        )
        with pytest.raises(OSError, match="final write failure"):
            await failing.complete(
                ModelRequest(provider="fake", prompt="hello", max_output_tokens=1),
                context=_context(run_id),
                usage_call_id="runtime-recovery",
            )

        recovering = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=_resolve_trace),
        )
        orchestrator = RunOrchestrator(
            storage=storage,
            event_bus=EventBus(sink=durable_sink, run_trace_resolver=_resolve_trace),
            executor_services={"model_invocation": recovering},
        )
        assert await orchestrator.recover_pending_usage_evidence() == 1
        assert await orchestrator.recover_pending_usage_evidence() == 0
        assert provider.calls == 1
        await _assert_settled_once(storage=storage, sink=durable_sink, run_id=run_id)

        worker_source = Path("templates/service-app/app/workers/runtime_worker.py").read_text(
            encoding="utf-8"
        )
        assert "await _recover_pending_usage(components)" in worker_source
    finally:
        await storage.dispose()
