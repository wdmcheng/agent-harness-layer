"""RUN-006 StreamingResponse 生命周期、背压与错误映射测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.test_runtime_checkpoint_runs_contracts import build_orchestrator

from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventType,
    LocalJsonlEventSink,
)
from agent_harness.runtime import RunReadAuthorization
from app.api.sse import format_sse_heartbeat, stream_run_events
from app.main import create_app


def event(seq: int, event_type: CanonicalEventType) -> CanonicalEvent:
    terminal = event_type in {
        CanonicalEventType.RUN_COMPLETED,
        CanonicalEventType.RUN_FAILED,
        CanonicalEventType.RUN_CANCELLED,
    }
    return CanonicalEvent(
        event_id=f"event-{seq}",
        tenant_id="default",
        run_id="run-sse",
        agent_id="fake-agent",
        event_type=event_type,
        seq=seq,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        payload={"seq": seq},
        terminal=terminal,
        visibility="public",
        request_id="event-request",
        trace_id="trace-sse",
    )


class OwnerOrchestrator:
    async def authorize_run_read(
        self,
        run_id: str,
        *,
        identity: object,
    ) -> RunReadAuthorization:
        if run_id != "run-sse":
            raise LookupError("run not found")
        return RunReadAuthorization(
            run_id=run_id,
            tenant_id="default",
            trace_id="trace-sse",
        )


class PagedReader:
    """模拟分页 reader，计数用于证明 transport 没有预取第二页。"""

    def __init__(
        self,
        events: list[CanonicalEvent],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.events = events
        self.failure = failure
        self.page_calls: list[int] = []

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int = 0,
        include_internal: bool = False,
        max_events: int = 100,
        max_bytes: int = 1_048_576,
    ) -> list[CanonicalEvent]:
        self.page_calls.append(after_seq)
        if self.failure is not None:
            raise self.failure
        return [item for item in self.events if item.seq > after_seq][:max_events]

    async def contains_seq(
        self,
        *,
        run_id: str,
        seq: int,
        include_internal: bool = False,
    ) -> bool:
        return any(item.seq == seq for item in self.events)

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool = False,
    ) -> CanonicalEvent | None:
        return next((item for item in self.events if item.terminal), None)


class RequestState:
    """为 generator 的公开 disconnect seam 提供确定性状态。"""

    def __init__(self, *states: bool) -> None:
        self.states = list(states)

    async def is_disconnected(self) -> bool:
        return self.states.pop(0) if self.states else False


def scope(
    *,
    run_id: str = "run-sse",
    last_event_id: int | None = None,
) -> dict[str, Any]:
    headers = [(b"x-request-id", b"req-stream")]
    if last_event_id is not None:
        headers.append((b"last-event-id", str(last_event_id).encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/v1/runs/{run_id}/events/stream",
        "raw_path": f"/api/v1/runs/{run_id}/events/stream".encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def receive_request() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_type",
    [
        CanonicalEventType.RUN_COMPLETED,
        CanonicalEventType.RUN_FAILED,
        CanonicalEventType.RUN_CANCELLED,
    ],
)
async def test_all_public_terminal_frames_close_the_stream(
    terminal_type: CanonicalEventType,
) -> None:
    reader = PagedReader([event(1, terminal_type)])
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await cast(Callable[..., Awaitable[None]], app)(scope(), receive_request, send)
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()

    assert f"event: {terminal_type.value}\n" in body
    assert body.count("data: ") == 1
    assert ": heartbeat" not in body
    assert reader.page_calls == [0]


@pytest.mark.asyncio
async def test_real_local_run_streams_through_authorized_reader(tmp_path: Path) -> None:
    """真实 local runtime 证明 ownership、sink page 与 SSE route 完整接通。"""

    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    try:
        created = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "sse-integration"},
            idempotency_key="sse-integration",
        )
        app = create_app(
            orchestrator=orchestrator,
            event_sink=LocalJsonlEventSink(events_path),
        )
        await cast(Callable[..., Awaitable[None]], app)(
            scope(run_id=created.run_id),
            receive_request,
            send,
        )
    finally:
        await storage.dispose()

    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()
    assert "event: run.started\n" in body
    assert "event: run.completed\n" in body
    assert body.count("data: ") == 2


@pytest.mark.asyncio
async def test_consumed_terminal_cursor_handshakes_then_immediately_eofs() -> None:
    reader = PagedReader([event(7, CanonicalEventType.RUN_COMPLETED)])
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await cast(Callable[..., Awaitable[None]], app)(scope(last_event_id=7), receive_request, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    assert start["status"] == 200
    assert body == b""
    assert reader.page_calls == []


@pytest.mark.asyncio
async def test_idle_stream_uses_comment_heartbeat_and_disconnect_skips_reads() -> None:
    idle_reader = PagedReader([])
    stream = stream_run_events(
        request=cast(Any, RequestState(False, False)),
        event_sink=cast(Any, idle_reader),
        run_id="run-sse",
        after_seq=0,
        include_internal=False,
        request_id="req-heartbeat",
        trace_id="trace-sse",
        heartbeat_interval_seconds=0,
    )

    assert await anext(stream) == format_sse_heartbeat()
    await stream.aclose()
    assert idle_reader.page_calls == [0]

    disconnected_reader = PagedReader([])
    disconnected = stream_run_events(
        request=cast(Any, RequestState(True)),
        event_sink=cast(Any, disconnected_reader),
        run_id="run-sse",
        after_seq=0,
        include_internal=False,
        request_id="req-disconnected",
        trace_id="trace-sse",
        heartbeat_interval_seconds=0,
    )
    with pytest.raises(StopAsyncIteration):
        await anext(disconnected)
    assert disconnected_reader.page_calls == []


@pytest.mark.asyncio
async def test_slow_asgi_send_and_cancellation_never_prefetch_the_second_page() -> None:
    events = [event(seq, CanonicalEventType.RUN_STARTED) for seq in range(1, 101)]
    events.append(event(101, CanonicalEventType.RUN_COMPLETED))
    reader = PagedReader(events)
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    first_frame_started = asyncio.Event()
    release_send = asyncio.Event()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_frame_started.set()
            await release_send.wait()

    async def invoke_app() -> None:
        await cast(Callable[..., Awaitable[None]], app)(scope(), receive_request, send)

    task = asyncio.create_task(invoke_app())
    await asyncio.wait_for(first_frame_started.wait(), timeout=1)

    assert reader.page_calls == [0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.page_calls == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "public_code"),
    [
        (RuntimeError("secret=provider-token"), "stream.internal_error"),
        (
            CanonicalEventEnvelopeStateInvalid("secret=oversized-row"),
            "stream.event_state_invalid",
        ),
    ],
)
async def test_post_handshake_failure_emits_one_redacted_error_then_closes(
    failure: Exception,
    public_code: str,
) -> None:
    reader = PagedReader([], failure=failure)
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await cast(Callable[..., Awaitable[None]], app)(scope(), receive_request, send)
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()
    data = next(
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    )

    assert body.count("event: stream.error") == 1
    assert data == {
        "code": public_code,
        "request_id": "req-stream",
        "trace_id": "trace-sse",
    }
    assert "secret" not in body
    assert reader.page_calls == [0]
