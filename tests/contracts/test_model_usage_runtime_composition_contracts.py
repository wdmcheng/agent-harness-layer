"""Model/embedding usage 在真实 runtime composition 中的关联合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    InMemoryRunQueue,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.runtime import build_runtime_components

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _event_payload(event: CanonicalEvent) -> dict[str, Any]:
    """读取 canonical payload，并把可空 envelope 边界显式收窄。"""

    assert event.payload is not None
    return event.payload


class _CountingModelProvider(FakeModelProvider):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        self.calls += 1
        return super().complete(request, model=model)


class _FailFinalOnceSink:
    """让 provider 结果先 durable，再在最终 usage 写前模拟进程中断。"""

    manages_event_capacity = False

    def __init__(self, delegate: LocalJsonlEventSink) -> None:
        self._delegate = delegate
        self._failed = False

    def bind_run_trace_resolver(self, resolver: Any) -> None:
        self._delegate.bind_run_trace_resolver(resolver)

    async def write(self, event: CanonicalEvent) -> CanonicalEvent:
        if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self._failed:
            self._failed = True
            raise OSError("injected usage final write failure")
        return await self._delegate.write(event)

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        return await self._delegate.read(run_id=run_id, after_seq=after_seq)

    async def latest_seq(self, run_id: str) -> int:
        return await self._delegate.latest_seq(run_id)

    async def has_terminal(self, run_id: str) -> bool:
        return await self._delegate.has_terminal(run_id)


@pytest.mark.asyncio
async def test_rag_runtime_composition_emits_correlated_model_and_embedding_usage(
    tmp_path: Path,
) -> None:
    """真实 RAG executor 必须经两个 invocation seam，且只使用 runtime 关联。"""

    database = tmp_path / "runtime-composition.db"
    events_path = tmp_path / "runtime-composition.jsonl"
    run_migrations(_dsn(database))
    components = build_runtime_components(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=_dsn(database),
        events_path=events_path,
        artifact_root=tmp_path / "artifacts",
    )
    try:
        result = await components.orchestrator.start_run(
            agent_id="examples.rag_assistant",
            input={
                "query": "system policy",
                "collection": "runtime-composition",
                "documents": [
                    {
                        "document_id": "usage-contract",
                        "content": "System policy evidence must keep canonical correlation.",
                        "source_ref": "docs://usage-contract",
                        "citation": "Usage Contract",
                    }
                ],
            },
            request_id="request-runtime-composition",
            trace_id="trace-runtime-composition",
        )
        assert result.status is RunStatus.COMPLETED

        sink = cast(LocalJsonlEventSink, components.event_sink)
        events = await sink.read(run_id=result.run_id)
        started = [
            event
            for event in events
            if event.event_type is CanonicalEventType.MODEL_REQUEST_STARTED
        ]
        final = [
            event for event in events if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED
        ]
        assert len(started) == len(final) == 2
        assert all(event.payload is not None for event in (*started, *final))

        started_by_kind = {
            cast(dict[str, Any], _event_payload(event)["usage"])["usage_kind"]: event
            for event in started
        }
        final_by_kind = {
            cast(dict[str, Any], _event_payload(event)["usage"])["usage_kind"]: event
            for event in final
        }
        assert set(started_by_kind) == set(final_by_kind) == {"model", "embedding"}

        call_ids: set[str] = set()
        for usage_kind in ("embedding", "model"):
            started_event = started_by_kind[usage_kind]
            final_event = final_by_kind[usage_kind]
            started_correlation = cast(dict[str, Any], _event_payload(started_event)["correlation"])
            final_correlation = cast(dict[str, Any], _event_payload(final_event)["correlation"])
            assert started_correlation == final_correlation
            assert started_correlation["usage_call_id"]
            call_ids.add(str(started_correlation["usage_call_id"]))
            for event in (started_event, final_event):
                assert event.tenant_id == IdentityContext.local_default().tenant_id
                assert event.run_id == result.run_id
                assert event.agent_id == "examples.rag_assistant"
                assert event.request_id == "request-runtime-composition"
                assert event.trace_id == "trace-runtime-composition"
                assert event.terminal is False
        assert len(call_ids) == 2
        assert max(event.seq for event in final) < events[-1].seq
        assert events[-1].terminal is True
    finally:
        await components.close()


@pytest.mark.asyncio
async def test_queued_execute_recovers_run_usage_before_executor_without_provider_replay(
    tmp_path: Path,
) -> None:
    """queued run 重放先补投确定结果，executor 不得观察到 usage 缺口。"""

    database = tmp_path / "queued-recovery.db"
    run_migrations(_dsn(database))
    storage = SQLAlchemyStorage.from_dsn(_dsn(database))
    queue = InMemoryRunQueue()
    sink = LocalJsonlEventSink(tmp_path / "queued-recovery.jsonl")
    provider = _CountingModelProvider()

    async def _resolve_trace(**_: object) -> str:
        return "trace-queued-recovery"

    model = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=_FailFinalOnceSink(sink),
            run_trace_resolver=_resolve_trace,
        ),
    )

    class _RecoveryObservingExecutor:
        def __init__(self) -> None:
            self.saw_recovered_usage = False

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            events = await sink.read(run_id=request.run_id)
            self.saw_recovered_usage = any(
                event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED for event in events
            )
            assert self.saw_recovered_usage is True
            assert provider.calls == 1
            assert context.trace_id == "trace-queued-recovery"
            return AgentExecutionResult.completed({"recovered": True})

        async def resume(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
            grant: object,
        ) -> AgentExecutionResult:
            del request, context, grant
            return AgentExecutionResult.failed("queued recovery fixture has no continuation")

    executor = _RecoveryObservingExecutor()
    identity = IdentityContext.local_default()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        queue=queue,
        executor_resolver=lambda _agent_id: executor,
        executor_services={"model_invocation": model},
        identity=identity,
    )
    try:
        submitted = await orchestrator.submit_run(
            agent_id="examples.recovery",
            input={"prompt": "recover without replay"},
            identity=identity,
            request_id="request-queued-recovery",
            trace_id="trace-queued-recovery",
        )
        usage_context = UsageEvidenceContext(
            tenant_id=identity.tenant_id,
            run_id=submitted.run_id,
            agent_id="examples.recovery",
            request_id="request-queued-recovery",
            trace_id="trace-queued-recovery",
        )
        with pytest.raises(OSError, match="usage final write failure"):
            await model.complete(
                ModelRequest(provider="fake", prompt="private", max_output_tokens=1),
                context=usage_context,
                usage_call_id=stable_usage_call_id(
                    context=usage_context,
                    operation_key="examples.recovery:model-primary",
                ),
            )

        delivery = await queue.pickup(consumer_id="worker-recovery")
        assert delivery is not None
        completed = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner-recovery",
            workflow_id="workflow-recovery",
        )
        events = await sink.read(run_id=submitted.run_id)
        event_types = [event.event_type.value for event in events]
        assert completed.status is RunStatus.COMPLETED
        assert executor.saw_recovered_usage is True
        assert provider.calls == 1
        assert event_types.index("model.usage.updated") < event_types.index("run.started")
        assert event_types[-1] == "run.completed"
    finally:
        await queue.close()
        await storage.dispose()
