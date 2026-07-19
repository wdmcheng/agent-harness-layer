"""SSE HTTP 传输层的授权、游标、背压与错误边界集成测试。"""

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
    """构造具有稳定请求上下文的公开事件，供传输层测试复用。

    ``terminal`` 必须由事件类型推导，而不是由调用者任意指定；这样测试中的
    流关闭行为与生产端对规范事件终态的判定保持一致。
    """

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
    """仅允许固定运行读取的最小编排器替身。

    路由在开始推流前会先调用读取授权 seam。本替身将未知运行明确视为不存在，
    避免测试因缺少授权检查而意外通过。
    """

    async def authorize_run_read(
        self,
        run_id: str,
        *,
        identity: object,
    ) -> RunReadAuthorization:
        """返回与测试事件一致的租户和追踪上下文，或拒绝未知运行。"""

        if run_id != "run-sse":
            raise LookupError("run not found")
        return RunReadAuthorization(
            run_id=run_id,
            tenant_id="default",
            trace_id="trace-sse",
        )


class PagedReader:
    """记录读取游标的分页事件仓储替身。

    ``page_calls`` 是背压断言的观测点：HTTP 层在上一帧尚未完成发送时不得读取
    下一页，否则慢客户端会把事件提前拉入内存并破坏取消语义。
    """

    def __init__(
        self,
        events: list[CanonicalEvent],
        *,
        failure: Exception | None = None,
    ) -> None:
        """保存预置事件或注入的读取异常，并初始化调用记录。"""

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
        """按严格大于游标的规则返回一页事件，模拟真实读取协议。

        未使用的参数仍保留在签名中，使替身覆盖生产 reader 的调用面；异常在
        首次读取时抛出，以验证握手完成后的公开错误映射。
        """

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
        """判断游标是否仍指向现有事件，用于终态游标的 EOF 快路径。"""

        return any(item.seq == seq for item in self.events)

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool = False,
    ) -> CanonicalEvent | None:
        """返回首个终态事件，模拟 reader 为恢复连接提供的终态查询。"""

        return next((item for item in self.events if item.terminal), None)


class RequestState:
    """为异步生成器的客户端断开 seam 提供确定性状态。

    每次调用按顺序消费一个状态值，从而能分别覆盖空闲心跳前仍连接，以及进入
    读取前已经断开的两种路径。
    """

    def __init__(self, *states: bool) -> None:
        """按调用顺序保存预设连接状态。"""

        self.states = list(states)

    async def is_disconnected(self) -> bool:
        """返回下一个断开状态；耗尽后默认保持连接。"""

        return self.states.pop(0) if self.states else False


def scope(
    *,
    run_id: str = "run-sse",
    last_event_id: int | None = None,
) -> dict[str, Any]:
    """构造与真实 SSE 路由匹配的最小 ASGI HTTP scope。

    ``last_event_id`` 仅在存在时编码为请求头，以区分首次订阅与断线续传，不让
    测试夹具通过虚构空头字段掩盖路由的解析分支。
    """

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
    """提供无请求体且已结束的 ASGI 接收消息。"""

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
    """每一种公开终态都只输出一帧数据并在首个分页结果后结束。"""

    reader = PagedReader([event(1, terminal_type)])
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集 ASGI 响应消息，保留真实发送顺序供后续拼接。"""

        messages.append(message)

    await cast(Callable[..., Awaitable[None]], app)(scope(), receive_request, send)
    # 仅合并 body 消息，避免状态行和终止空帧干扰 SSE 文本断言。
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()

    assert f"event: {terminal_type.value}\n" in body
    assert body.count("data: ") == 1
    assert ": heartbeat" not in body
    assert reader.page_calls == [0]


@pytest.mark.asyncio
async def test_real_local_run_streams_through_authorized_reader(tmp_path: Path) -> None:
    """真实本地运行验证授权、事件仓储分页与 SSE 路由完整接通。"""

    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集真实应用写出的响应片段，而不绕过 ASGI transport。"""

        messages.append(message)

    try:
        # 通过真实编排器生成事件，避免仅依赖替身而漏掉本地持久化接线。
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
    """终态事件已被客户端消费时仍成功握手，但不再读取或重放事件。"""

    reader = PagedReader([event(7, CanonicalEventType.RUN_COMPLETED)])
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集响应，以区分握手状态行与空的流体。"""

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
    """空闲连接发送协议心跳；已断开的客户端在读取前直接结束。"""

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

    # 注释型心跳不会伪装成业务事件，客户端可安全忽略它。
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
    """慢发送和任务取消期间不得预取下一页，保证传输层背压。"""

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
        """阻塞首个数据帧的发送，制造可观测的慢客户端窗口。"""

        if message["type"] == "http.response.body" and message.get("body"):
            first_frame_started.set()
            await release_send.wait()

    async def invoke_app() -> None:
        """在独立任务中运行 ASGI 应用，便于从外部取消。"""

        await cast(Callable[..., Awaitable[None]], app)(scope(), receive_request, send)

    task = asyncio.create_task(invoke_app())
    await asyncio.wait_for(first_frame_started.wait(), timeout=1)

    # 首帧尚未发出时只允许读取首个游标页；取消后也不应补读。
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
    """握手后读取失败只发一个脱敏公开错误，随后关闭流。"""

    reader = PagedReader([], failure=failure)
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        """收集错误帧，验证 transport 未把底层异常泄露给客户端。"""

        messages.append(message)

    await cast(Callable[..., Awaitable[None]], app)(scope(), receive_request, send)
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()
    # 从单个 SSE 数据行恢复 JSON，分别验证机器可读错误码与敏感信息隔离。
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
