"""把授权 CanonicalEvent reader 适配成有界 SSE response body。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventEnvelopeTooLarge,
    EventReader,
    canonical_event_bytes,
    canonical_json_bytes,
)
from agent_harness.events.sinks.base import DEFAULT_EVENT_PAGE_SIZE, MAX_EVENT_PAGE_BYTES

HEARTBEAT_INTERVAL_SECONDS = 15.0
MAX_EVENT_SEQ = 2_147_483_647


def format_sse_event(event: CanonicalEvent) -> str:
    """使用公共 canonical serializer 生成一个完整业务 frame。"""

    data = canonical_event_bytes(event).decode("utf-8")
    return f"id: {event.seq}\nevent: {event.event_type.value}\ndata: {data}\n\n"


def format_sse_heartbeat() -> str:
    """心跳只使用 comment，不占用业务 cursor。"""

    return ": heartbeat\n\n"


def format_sse_stream_error(*, request_id: str, trace_id: str, code: str) -> str:
    """握手后只公开稳定关联字段，绝不序列化内部异常文本。"""

    data = canonical_json_bytes(
        {
            "code": code,
            "request_id": request_id,
            "trace_id": trace_id,
        }
    ).decode("utf-8")
    return f"event: stream.error\ndata: {data}\n\n"


async def validate_stream_cursor(
    *,
    request: Request,
    event_sink: EventReader,
    run_id: str,
    last_event_id: int,
    include_internal: bool,
) -> None:
    """在握手前校验唯一 cursor 及当前授权可见视图的 membership。"""

    raw_cursor_headers = request.headers.getlist("last-event-id")
    if "after_seq" in request.query_params:
        raise _stream_validation_error(
            "query",
            "after_seq",
            request.query_params.get("after_seq"),
        )
    if raw_cursor_headers and (
        len(raw_cursor_headers) != 1
        or not raw_cursor_headers[0]
        or any(not "0" <= character <= "9" for character in raw_cursor_headers[0])
    ):
        raise _stream_validation_error("header", "Last-Event-ID", raw_cursor_headers)
    if last_event_id != 0 and not await event_sink.contains_seq(
        run_id=run_id,
        seq=last_event_id,
        include_internal=include_internal,
    ):
        raise _stream_validation_error("header", "Last-Event-ID", last_event_id)


def _stream_validation_error(location: str, field: str, value: object) -> RequestValidationError:
    """把 cursor 约束送入 service app 既有 422 envelope handler。"""

    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": (location, field),
                "msg": "Value error, invalid event stream cursor",
                "input": value,
                "ctx": {"error": ValueError("invalid event stream cursor")},
            }
        ]
    )


async def stream_run_events(
    *,
    request: Request,
    event_sink: EventReader,
    run_id: str,
    after_seq: int,
    include_internal: bool,
    request_id: str,
    trace_id: str,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncGenerator[str, None]:
    """逐页读取、逐 frame 交还控制权，并在 terminal 或取消时收口。

    ``StreamingResponse`` 每完成一次 ASGI send 才会请求下一个 iterator item，
    因此一个 page 内逐条 yield 就是 transport 的背压边界；只有当前 page 已全部
    消费后才会再次调用 ``read_page``，不会为慢客户端预取第二页。
    """

    cursor = after_seq
    try:
        terminal = await event_sink.terminal_event(
            run_id=run_id,
            include_internal=include_internal,
        )
        if terminal is not None and terminal.seq <= cursor:
            return

        while True:
            if await request.is_disconnected():
                return
            page = await event_sink.read_page(
                run_id=run_id,
                after_seq=cursor,
                include_internal=include_internal,
                max_events=DEFAULT_EVENT_PAGE_SIZE,
                max_bytes=MAX_EVENT_PAGE_BYTES,
            )
            if page:
                for event in page:
                    if await request.is_disconnected():
                        return
                    frame = format_sse_event(event)
                    yield frame
                    cursor = event.seq
                    if event.terminal:
                        return
                continue

            terminal = await event_sink.terminal_event(
                run_id=run_id,
                include_internal=include_internal,
            )
            if terminal is not None:
                if terminal.seq <= cursor:
                    return
                # 可见 terminal 尚未消费却返回空页，表示 reader 状态不一致。
                raise RuntimeError("event reader omitted a visible terminal event")

            await asyncio.sleep(heartbeat_interval_seconds)
            if await request.is_disconnected():
                return
            yield format_sse_heartbeat()
    except asyncio.CancelledError:
        # send cancellation 必须原样传播，交由 ASGI server 立即释放连接资源。
        raise
    except Exception as exc:
        code = (
            "stream.event_state_invalid"
            if isinstance(
                exc,
                CanonicalEventEnvelopeStateInvalid | CanonicalEventEnvelopeTooLarge,
            )
            else "stream.internal_error"
        )
        yield format_sse_stream_error(
            request_id=request_id,
            trace_id=trace_id,
            code=code,
        )


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "MAX_EVENT_SEQ",
    "format_sse_event",
    "format_sse_heartbeat",
    "format_sse_stream_error",
    "stream_run_events",
    "validate_stream_cursor",
]
