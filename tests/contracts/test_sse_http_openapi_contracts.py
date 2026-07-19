"""RUN-006 HTTP、cursor 与 OpenAPI 的公开合同测试。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from agent_harness.events import CanonicalEvent, CanonicalEventType
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import RunReadAuthorization
from app.main import create_app


def event(
    seq: int,
    event_type: CanonicalEventType,
    *,
    visibility: str = "public",
) -> CanonicalEvent:
    """构造 RUN-003/RUN-006 共用的稳定 event fixture。"""

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
        visibility=visibility,
        request_id="event-request",
        trace_id="trace-sse",
    )


class OwnerOrchestrator:
    """只暴露 route 所需 ownership seam，避免测试越过公开适配边界。"""

    def __init__(self, *, missing: bool = False) -> None:
        """选择返回固定运行授权或模拟运行不可见。"""

        self.missing = missing

    async def authorize_run_read(
        self,
        run_id: str,
        *,
        identity: object,
    ) -> RunReadAuthorization:
        """只为固定运行返回授权上下文，其他运行统一按不存在处理。"""

        if self.missing or run_id != "run-sse":
            raise LookupError("run not found")
        return RunReadAuthorization(
            run_id=run_id,
            tenant_id="default",
            trace_id="trace-sse",
        )


class ReaderSpy:
    """记录 HTTP 是否只调用冻结后的授权 reader seam。"""

    def __init__(self, events: list[CanonicalEvent]) -> None:
        """保存预置事件并初始化 cursor 与分页调用记录。"""

        self.events = events
        self.contains_calls: list[tuple[str, int, bool]] = []
        self.page_calls: list[tuple[str, int, bool, int, int]] = []

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        """按运行和严格游标筛选事件，供 JSON 与 SSE 公开 envelope 对比。"""

        return [item for item in self.events if item.run_id == run_id and item.seq > after_seq]

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int = 0,
        include_internal: bool = False,
        max_events: int = 100,
        max_bytes: int = 1_048_576,
    ) -> list[CanonicalEvent]:
        """记录分页参数后按可见性和游标返回事件，模拟授权 reader 的协议。"""

        self.page_calls.append((run_id, after_seq, include_internal, max_events, max_bytes))
        visible = [
            item
            for item in self.events
            if item.run_id == run_id
            and item.seq > after_seq
            and (include_internal or item.visibility == "public")
        ]
        return visible[:max_events]

    async def contains_seq(
        self,
        *,
        run_id: str,
        seq: int,
        include_internal: bool = False,
    ) -> bool:
        """记录 cursor 成员查询，并仅在同一运行和可见性视图内返回真值。"""

        self.contains_calls.append((run_id, seq, include_internal))
        return any(
            item.run_id == run_id
            and item.seq == seq
            and (include_internal or item.visibility == "public")
            for item in self.events
        )

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool = False,
    ) -> CanonicalEvent | None:
        """返回同一可见性视图下首个终态事件，支持连接 EOF 判断。"""

        return next(
            (
                item
                for item in self.events
                if item.run_id == run_id
                and item.terminal
                and (include_internal or item.visibility == "public")
            ),
            None,
        )


async def asgi_get(
    app: Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Any]],
        Awaitable[None],
    ],
    path: str,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """直接驱动 ASGI，保留 streaming headers 和完整 frame bytes。"""

    messages: list[dict[str, Any]] = []
    delivered_request = False

    async def receive() -> dict[str, Any]:
        """先发送一次空 HTTP 请求体，随后保持已结束状态以满足 ASGI 协议。"""

        nonlocal delivered_request
        if not delivered_request:
            delivered_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        """按发送顺序收集 ASGI 响应消息，供状态、头和流体断言。"""

        messages.append(message)

    raw_path, _, query = path.partition("?")
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "headers": [(b"x-request-id", b"req-sse"), *headers],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return cast(int, start["status"]), cast(list[tuple[bytes, bytes]], start["headers"]), body


def test_run_006_openapi_is_bidirectionally_exact() -> None:
    """SSE 操作的参数、边界与响应码必须与公开 HTTP 契约双向一致。"""

    reader = ReaderSpy([])
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )
    operation = app.openapi()["paths"]["/api/v1/runs/{run_id}/events/stream"]["get"]
    parameters = {(item["in"], item["name"]): item for item in operation["parameters"]}

    assert set(parameters) == {
        ("path", "run_id"),
        ("query", "include_internal"),
        ("header", "Accept"),
        ("header", "Last-Event-ID"),
    }
    cursor_schema = parameters[("header", "Last-Event-ID")]["schema"]
    assert cursor_schema["minimum"] == 0
    assert cursor_schema["maximum"] == 2_147_483_647
    assert parameters[("query", "include_internal")]["schema"]["default"] is False
    assert set(operation["responses"]) == {"200", "401", "403", "404", "422", "500"}
    assert set(operation["responses"]["200"]["content"]) == {"text/event-stream"}
    for status in ("401", "403", "404", "422", "500"):
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/ApiErrorEnvelope")


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [b"-1", b"1.0", b"+1", b"2147483648", b"not-a-seq"])
async def test_invalid_last_event_id_uses_one_pre_handshake_envelope(cursor: bytes) -> None:
    """非法 cursor 必须在 SSE 握手前返回唯一 JSON 校验错误，不能触碰 reader。"""

    reader = ReaderSpy([event(1, CanonicalEventType.RUN_COMPLETED)])
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )

    status, headers, body = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", cursor)],
    )

    assert status == 422
    assert dict(headers)[b"content-type"].startswith(b"application/json")
    assert json.loads(body)["error"]["code"] == "validation_error"
    assert reader.contains_calls == []


@pytest.mark.asyncio
async def test_cursor_membership_visibility_and_after_seq_do_not_form_an_oracle() -> None:
    """不可见、缺失、跨运行和超范围 cursor 都用相同校验响应，避免信息探测。"""

    reader = ReaderSpy(
        [
            event(1, CanonicalEventType.REASONING_DELTA, visibility="internal"),
            event(3, CanonicalEventType.RUN_COMPLETED),
            event(4, CanonicalEventType.RUN_COMPLETED).model_copy(update={"run_id": "other-run"}),
        ]
    )
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )

    hidden = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"1")],
    )
    missing = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"2")],
    )
    other_run = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"4")],
    )
    beyond_visible_range = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"99")],
    )
    second_cursor = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream?after_seq=0",
    )

    # 所有不可用 cursor 的公开外观相同；仅 spy 证明服务内部确实按同一可见性视图查询。
    for status, headers, body in (
        hidden,
        missing,
        other_run,
        beyond_visible_range,
        second_cursor,
    ):
        assert status == 422
        assert dict(headers)[b"content-type"].startswith(b"application/json")
        assert json.loads(body)["error"]["code"] == "validation_error"
    assert reader.contains_calls == [
        ("run-sse", 1, False),
        ("run-sse", 2, False),
        ("run-sse", 4, False),
        ("run-sse", 99, False),
    ]


@pytest.mark.asyncio
async def test_missing_zero_and_visible_cursor_resume_with_streaming_headers() -> None:
    """首次订阅、零 cursor 与有效续传应产生正确帧集合和禁止缓冲的流式响应头。"""

    reader = ReaderSpy(
        [
            event(1, CanonicalEventType.RUN_STARTED),
            event(2, CanonicalEventType.RUN_COMPLETED),
        ]
    )
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )

    initial = await asgi_get(cast(Any, app), "/api/v1/runs/run-sse/events/stream")
    explicit_zero = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"0")],
    )
    resumed = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"1")],
    )
    leading_zero = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream",
        headers=[(b"last-event-id", b"01")],
    )

    assert initial[2] == explicit_zero[2]
    assert initial[2].count(b"data: ") == 2
    assert resumed[2].count(b"data: ") == 1
    assert b"id: 1\n" not in resumed[2]
    assert b"id: 2\n" in resumed[2]
    assert leading_zero[2] == resumed[2]
    headers = dict(resumed[1])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert headers[b"cache-control"] == b"no-cache"
    assert headers[b"x-accel-buffering"] == b"no"
    assert reader.contains_calls == [
        ("run-sse", 1, False),
        ("run-sse", 1, False),
    ]


@pytest.mark.asyncio
async def test_authorized_internal_cursor_uses_the_same_visibility_view() -> None:
    """经授权读取内部事件时，cursor 校验与后续分页必须使用同一内部可见性视图。"""

    reader = ReaderSpy(
        [
            event(1, CanonicalEventType.REASONING_DELTA, visibility="internal"),
            event(2, CanonicalEventType.RUN_COMPLETED),
        ]
    )
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )

    status, _, body = await asgi_get(
        cast(Any, app),
        "/api/v1/runs/run-sse/events/stream?include_internal=true",
        headers=[(b"last-event-id", b"1")],
    )

    assert status == 200
    assert b"id: 1\n" not in body
    assert b"id: 2\n" in body
    assert reader.contains_calls == [("run-sse", 1, True)]


@pytest.mark.asyncio
async def test_include_internal_policy_and_run_ownership_fail_before_sse_headers() -> None:
    """内部事件权限或运行归属失败必须在建立 SSE 响应头前返回 JSON 错误。"""

    reader = ReaderSpy([event(1, CanonicalEventType.RUN_COMPLETED)])
    denied_app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
        policy_engine=PolicyEngine(
            provider=YamlPolicyProvider(deny_actions={"events.read_internal"})
        ),
    )
    missing_app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator(missing=True)),
        event_sink=cast(Any, reader),
    )

    denied = await asgi_get(
        cast(Any, denied_app),
        "/api/v1/runs/run-sse/events/stream?include_internal=true",
    )
    missing = await asgi_get(
        cast(Any, missing_app),
        "/api/v1/runs/other-tenant-run/events/stream",
    )

    assert denied[0] == 403
    assert json.loads(denied[2])["error"]["code"] == "policy.denied"
    assert missing[0] == 404
    assert json.loads(missing[2])["error"]["code"] == "api.not_found"
    assert reader.page_calls == []


@pytest.mark.asyncio
async def test_run_003_and_run_006_expose_the_same_public_envelopes() -> None:
    """JSON 事件查询与 SSE 流对同一公开事件必须输出等价 canonical envelope。"""

    reader = ReaderSpy(
        [
            event(1, CanonicalEventType.RUN_STARTED),
            event(2, CanonicalEventType.REASONING_DELTA, visibility="internal"),
            event(3, CanonicalEventType.RUN_COMPLETED),
        ]
    )
    app = create_app(
        orchestrator=cast(Any, OwnerOrchestrator()),
        event_sink=cast(Any, reader),
    )

    json_response = await asgi_get(cast(Any, app), "/api/v1/runs/run-sse/events")
    sse_response = await asgi_get(cast(Any, app), "/api/v1/runs/run-sse/events/stream")
    json_events = json.loads(json_response[2])["events"]
    sse_events = [
        json.loads(line.removeprefix("data: "))
        for line in sse_response[2].decode().splitlines()
        if line.startswith("data: ")
    ]

    assert json_response[0] == sse_response[0] == 200
    assert sse_events == [CanonicalEvent.model_validate(item).to_payload() for item in json_events]
    assert [item["event_type"] for item in sse_events] == ["run.started", "run.completed"]
