"""可信 bound façade 到 durable CanonicalEvent 的重放运行时合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_streaming_sdk_event_test_helpers import (
    AgentRunResultEvent,
    PartStartEvent,
    TextPart,
    patch_pydantic_stream_event_types,
)
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run
from tests.contracts.test_controlled_model_streaming_routing_contracts import (
    _policy,
    _request,
    _stream_settings,
)

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import stream_group_id


class _ReplaySDKAgent:
    """统计真实 adapter 的 SDK 流启动次数，供耐久重放零副作用断言。"""

    def __init__(self) -> None:
        self.iterations = 0

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        del model_settings

        async def events():  # type: ignore[no-untyped-def]
            self.iterations += 1

            class Result:
                output = prompt

                def usage(self) -> object:
                    return type("Usage", (), {"input_tokens": 1, "output_tokens": 1})()

            result = Result()
            yield PartStartEvent(index=0, part=TextPart(content=prompt))
            yield AgentRunResultEvent(result=cast(Any, result))

        yield events()


class _FailOnceCompletedSink(LocalJsonlEventSink):
    """首次 completed 公开写入失败，模拟耐久提交后的进程/transport 中断。"""

    failed_completed = False

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        if (
            event.event_type is CanonicalEventType.MODEL_OUTPUT_COMPLETED
            and not self.failed_completed
        ):
            self.failed_completed = True
            raise RuntimeError("injected completed publication failure")
        return await super().write(event, after_claim=after_claim)


@pytest.mark.asyncio
async def test_completed_publication_failure_recovers_durable_usage_without_provider_replay(
    tmp_path: Path,
) -> None:
    """completed 首次公开失败时，usage 与预算已经耐久，恢复只补投、不重放 provider。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-completed-recovery.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = _FailOnceCompletedSink(
        tmp_path / "stream-completed-recovery.jsonl",
        run_trace_resolver=resolve_trace,
    )
    provider = FakeModelProvider()
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        with pytest.raises(RuntimeError):
            await bound.stream(
                ModelRequest(
                    capability="text_stream",
                    prompt="recover durable completion",
                    max_output_tokens=8,
                ),
                operation_key="completed-publication-failure",
            )

        events = await sink.read(run_id=run_id)
        started_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started_payload["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            before_group_states = [item.state for item in group]
            before_usage_state = usage.state

        assert provider.stream_pull_count == 1
        assert before_group_states[-1] == "result_persisted"
        assert before_usage_state == "result_persisted"
        assert CanonicalEventType.MODEL_OUTPUT_COMPLETED not in {
            event.event_type for event in events
        }
        assert CanonicalEventType.MODEL_USAGE_UPDATED not in {event.event_type for event in events}

        assert await service.recover_pending(run_id=run_id) == 2
        recovered_events = await sink.read(run_id=run_id)
        assert [event.event_type for event in recovered_events[-2:]] == [
            CanonicalEventType.MODEL_OUTPUT_COMPLETED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert provider.stream_pull_count == 1
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            completed_state = group[-1].state
            usage_state = usage.state
            outstanding = capacity.outstanding_reserved_event_count
        assert completed_state == "published"
        assert usage_state == "published"
        assert outstanding == 0
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_controlled_stream_replay_validates_text_stream_route_without_provider_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 settlement 的第二次 bound 调用只恢复 response/event，不重新迭代 SDK。"""

    patch_pydantic_stream_event_types(monkeypatch)
    settings = _stream_settings()
    agent = _ReplaySDKAgent()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-replay.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "stream-replay.jsonl", run_trace_resolver=resolve_trace)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_provider="openai-compatible",
                default_model="fixture-text-1",
            ),
            providers={"openai-compatible": provider},
            model_settings=settings.model,
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        request = _request().model_copy(update={"prompt": "hello"})
        first = await bound.stream(request, operation_key="controlled-stream")
        replay = await bound.stream(request, operation_key="controlled-stream")

        assert replay == first
        assert agent.iterations == 1
        assert len(await sink.read(run_id=run_id)) == 4
    finally:
        await service.aclose()
        await storage.dispose()
