"""真实模型增量 smoke 的同进程时延探针与 runtime executor。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import cast

from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
from agent_harness.models import (
    BoundModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
)
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)

type ASGIMessage = dict[str, object]
type ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
type ASGISend = Callable[[ASGIMessage], Awaitable[None]]
type ASGIApp = Callable[[dict[str, object], ASGIReceive, ASGISend], Awaitable[None]]
type ServiceAppFactory = Callable[..., ASGIApp]


class StreamTimingRecorder:
    """只记录封闭阶段的首次 monotonic 时刻，不接收文本或 provider 对象。"""

    def __init__(self, *, clock: Callable[[], float] = perf_counter) -> None:
        self._clock = clock
        self._origin: float | None = None
        self.provider_first_delta_ms: int | None = None
        self.committed_first_delta_ms: int | None = None
        self.client_first_delta_ms: int | None = None

    def observe(self, stage: str) -> None:
        """以 origin 为共同基准，重复阶段保持首次观察。"""

        now = self._clock()
        if stage == "origin":
            if self._origin is None:
                self._origin = now
            return
        if self._origin is None:
            raise RuntimeError("stream timing origin is not established")
        field = {
            "provider_delta": "provider_first_delta_ms",
            "committed_delta": "committed_first_delta_ms",
            "client_delta": "client_first_delta_ms",
        }.get(stage)
        if field is None:
            raise ValueError("unknown stream timing stage")
        if getattr(self, field) is None:
            setattr(self, field, max(0, int(round((now - self._origin) * 1000))))


async def measure_existing_sse_first_frame(
    app: ASGIApp,
    *,
    run_id: str,
    clock: Callable[[], float] = perf_counter,
) -> int:
    """驱动真实 RUN-006 ASGI 路由，并只以首个非空 SSE frame 停止计时。"""

    if not run_id or "/" in run_id:
        raise ValueError("SSE smoke run id is invalid")
    started = clock()
    response_status: list[int] = []
    response_headers: list[tuple[bytes, bytes]] = []
    first_frame_ms: int | None = None
    delivered_request = False

    async def receive() -> ASGIMessage:
        """发送一次已结束 GET body；后续轮询保持同一 ASGI 请求状态。"""

        nonlocal delivered_request
        if not delivered_request:
            delivered_request = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: ASGIMessage) -> None:
        """验证握手并在首个真实 frame 到达时记录 elapsed。"""

        nonlocal response_headers, first_frame_ms
        if message["type"] == "http.response.start":
            response_status.append(cast(int, message["status"]))
            response_headers = cast(list[tuple[bytes, bytes]], message["headers"])
            return
        if message["type"] == "http.response.body" and message.get("body"):
            if first_frame_ms is None:
                first_frame_ms = max(0, int(round((clock() - started) * 1000)))

    path = f"/api/v1/runs/{run_id}/events/stream"
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"accept", b"text/event-stream"),
                (b"x-request-id", b"live-stream-smoke-sse"),
            ],
            "client": ("live-stream-smoke", 50000),
            "server": ("in-process", 80),
        },
        receive,
        send,
    )
    headers = {key.lower(): value for key, value in response_headers}
    if (
        response_status != [200]
        or not headers.get(b"content-type", b"").startswith(b"text/event-stream")
        or first_frame_ms is None
    ):
        raise RuntimeError("existing committed event SSE probe produced no first frame")
    return first_frame_ms


def service_app_factory(service_root: Path) -> ServiceAppFactory:
    """从仓库 service-app 模板加载真实 app factory，不复制 RUN-006 适配逻辑。"""

    root = service_root.resolve()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    module = importlib.import_module("app.main")
    module_path = Path(cast(str, module.__file__)).resolve()
    if not module_path.is_relative_to(root):
        raise RuntimeError("service-app factory resolved outside the trusted template root")
    return cast(ServiceAppFactory, module.create_app)


class LiveStreamSmokeExecutor:
    """经 bound invocation stream 执行固定调用，并并发观察 committed delta。"""

    def __init__(
        self,
        *,
        request: ModelRequest,
        sink: LocalJsonlEventSink,
        recorder: StreamTimingRecorder,
    ) -> None:
        self._request = request
        self._sink = sink
        self._recorder = recorder
        self.response: ModelResponse | None = None
        self.error: ModelProviderInvocationError | None = None

    async def _observe_client(self, *, run_id: str, done: asyncio.Event) -> None:
        """只在 commit 时钟已记录后确认 client 观察，不持有 provider iterator。"""

        while True:
            events = await self._sink.read(run_id=run_id)
            delta_visible = any(
                event.event_type is CanonicalEventType.MODEL_OUTPUT_DELTA for event in events
            )
            if delta_visible and self._recorder.committed_first_delta_ms is not None:
                self._recorder.observe("client_delta")
                return
            if done.is_set():
                return
            await asyncio.sleep(0)

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """调用可信 façade；业务 executor 不取得 provider 或 SDK stream。"""

        invocation = cast(
            BoundModelInvocationService,
            context.require_service("model_invocation"),
        )
        done = asyncio.Event()
        watcher = asyncio.create_task(self._observe_client(run_id=request.run_id, done=done))
        try:
            self.response = await invocation.stream(
                self._request,
                operation_key="authorized-live-stream-smoke",
            )
        except ModelProviderInvocationError as exc:
            self.error = exc
            return AgentExecutionResult.failed(exc.code)
        finally:
            done.set()
            await watcher
        return AgentExecutionResult.completed({"completed": True})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """live stream smoke 不产生审批等待，意外 resume 必须关闭失败。"""

        del request, context, grant
        return AgentExecutionResult.failed("live stream smoke does not support resume")


__all__ = [
    "LiveStreamSmokeExecutor",
    "StreamTimingRecorder",
    "measure_existing_sse_first_frame",
    "service_app_factory",
]
