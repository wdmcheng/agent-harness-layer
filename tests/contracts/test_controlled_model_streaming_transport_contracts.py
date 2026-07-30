"""SSE/CLI 共用 committed reader 的游标与 ownership 合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


class _CountingFakeProvider(FakeModelProvider):
    """只统计 invocation-owned stream prepare；reader 不得取得本对象。"""

    def __init__(self) -> None:
        self.prepare_calls = 0

    async def prepare_stream(self, request: ModelRequest, *, plan: object):  # type: ignore[no-untyped-def]
        self.prepare_calls += 1
        return await super().prepare_stream(request, plan=plan)


@pytest.mark.asyncio
async def test_public_and_internal_reconnect_read_same_committed_stream_without_provider_replay(
    tmp_path: Path,
) -> None:
    """同一耐久流按权限恢复公开/内部事件，reader 断开不会重放 provider。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-transport.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-transport.jsonl", run_trace_resolver=resolve_trace
    )
    bus = EventBus(
        sink=sink,
        run_trace_resolver=resolve_trace,
        capacity_storage=storage,
    )
    provider = _CountingFakeProvider()
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=bus,
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
        await bound.stream(
            ModelRequest(capability="text_stream", prompt="transport", max_output_tokens=8),
            operation_key="transport-stream",
        )
        await bus.publish(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            event_type=CanonicalEventType.RUN_COMPLETED,
            terminal=True,
            visibility="public",
            request_id="request-a",
            trace_id="trace-a",
        )
        assert provider.prepare_calls == 1

        first_frame = await sink.read_page(
            run_id=run_id,
            after_seq=0,
            include_internal=False,
            max_events=1,
        )
        assert [event.event_type for event in first_frame] == [
            CanonicalEventType.MODEL_OUTPUT_DELTA
        ]
        calls_at_disconnect = provider.prepare_calls

        resumed = await sink.read_page(
            run_id=run_id,
            after_seq=first_frame[0].seq,
            include_internal=False,
        )
        assert [event.event_type for event in resumed] == [
            CanonicalEventType.MODEL_OUTPUT_COMPLETED,
            CanonicalEventType.RUN_COMPLETED,
        ]
        assert provider.prepare_calls == calls_at_disconnect == 1

        internal_first_page = await sink.read_page(
            run_id=run_id,
            after_seq=0,
            include_internal=True,
            max_events=2,
        )
        assert [event.event_type for event in internal_first_page] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_OUTPUT_DELTA,
        ]
        internal_calls_at_disconnect = provider.prepare_calls

        internal_resumed = await sink.read_page(
            run_id=run_id,
            after_seq=internal_first_page[-1].seq,
            include_internal=True,
        )
        assert [event.event_type for event in internal_resumed] == [
            CanonicalEventType.MODEL_OUTPUT_COMPLETED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
            CanonicalEventType.RUN_COMPLETED,
        ]
        assert provider.prepare_calls == internal_calls_at_disconnect == 1
    finally:
        await service.aclose()
        await storage.dispose()
