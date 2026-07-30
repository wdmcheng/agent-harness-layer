"""Pydantic AI 文本流的惰性迭代、关闭与 provider-neutral 证据转换。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from time import perf_counter
from types import TracebackType
from typing import Protocol, cast

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.settings import ModelSettings as PydanticModelSettings

from agent_harness.adapters.models._pydantic_ai_client import ModelProviderError
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelStreamCloseResult,
    ModelStreamDelta,
    ModelStreamUsage,
)
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.streaming import MAX_STREAM_COLLECTOR_UTF8_BYTES


class AgentRunResult(Protocol):
    """隔离 Pydantic AI result 所需的最小表面。"""

    output: object

    def usage(self) -> object:
        """返回 provider SDK usage；只允许 adapter 边界读取。"""
        ...


class _FinalResultEvent(Protocol):
    """收窄 SDK final event，避免泛型 result 的 unknown 类型越过 adapter。"""

    result: object


class StreamEventContext(Protocol):
    """SDK 惰性 stream context 的最小生命周期表面。"""

    async def __aenter__(self) -> AsyncIterator[object]: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> bool | None: ...


class PydanticStreamAgent(Protocol):
    """只暴露文本流需要的 SDK event stream 入口。"""

    def run_stream_events(
        self,
        prompt: str,
        *,
        model_settings: object,
    ) -> StreamEventContext: ...


TokenUsageReader = Callable[[AgentRunResult], dict[str, int]]
CostEstimator = Callable[[ModelRoutePlan, dict[str, int]], float | None]
StreamUnregister = Callable[["PreparedPydanticStream"], Awaitable[None]]
ClientCloser = Callable[[], Awaitable[None]]


def _empty_text_fragments() -> list[str]:
    """为每个 prepared stream 提供独立文本列表，并保留精确元素类型。"""

    return []


def _empty_token_usage() -> dict[str, int]:
    """为每个 prepared stream 提供独立且已收窄的 usage 缓存。"""

    return {}


@dataclass
class PreparedPydanticStream:
    """锁定 SDK 惰性、文本白名单、唯一 final 与单次 usage 读取语义。"""

    provider_id: str
    request: ModelRequest
    plan: ModelRoutePlan
    agent: PydanticStreamAgent
    permit: asyncio.Semaphore
    deadline: float
    started_at: float
    token_usage_reader: TokenUsageReader
    cost_estimator: CostEstimator
    unregister: StreamUnregister | None = None
    _closed: bool = False
    _started: bool = False
    _finished: bool = False
    _context: StreamEventContext | None = None
    _context_exited: bool = False
    _close_uncertain: bool = False
    _result: AgentRunResult | None = None
    _observed_fragments: list[str] = dataclass_field(default_factory=_empty_text_fragments)
    _observed_utf8_bytes: int = 0
    _usage_payload: dict[str, int] = dataclass_field(default_factory=_empty_token_usage)
    _usage_read: bool = False
    _usage_invalid: bool = False
    _iteration_task: asyncio.Task[object] | None = None
    _close_owner_task: asyncio.Task[object] | None = None
    _close_lock: asyncio.Lock = dataclass_field(default_factory=asyncio.Lock)
    _close_complete: asyncio.Event = dataclass_field(default_factory=asyncio.Event)

    def __aiter__(self) -> AsyncIterator[ModelStreamDelta]:
        if self._closed or self._started:
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            )
        self._started = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ModelStreamDelta]:
        """只转发 TextPart start/delta；part-end、reasoning、tool 等留在 adapter。"""

        current_task = asyncio.current_task()
        if current_task is not None:
            self._iteration_task = current_task
        loop = asyncio.get_running_loop()
        remaining = self.deadline - loop.time()
        if remaining <= 0:
            raise ModelProviderError("model.invocation_cancelled", side_effect_state="not_started")
        context = self.agent.run_stream_events(
            self.request.prompt,
            model_settings=PydanticModelSettings(max_tokens=self.plan.output_token_cap),
        )
        self._context = context
        events = await context.__aenter__()
        final_count = 0
        try:
            async with asyncio.timeout(remaining):
                async for event in events:
                    if isinstance(event, AgentRunResultEvent):
                        final_count += 1
                        if final_count != 1:
                            raise ModelProviderError(
                                "model.provider_side_effect_unknown",
                                side_effect_state="unknown",
                            )
                        final_event = cast(_FinalResultEvent, event)
                        self._result = cast(AgentRunResult, final_event.result)
                        continue
                    text = None
                    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                        text = event.part.content
                    elif isinstance(event, PartDeltaEvent) and isinstance(
                        event.delta, TextPartDelta
                    ):
                        text = event.delta.content_delta
                    if text:
                        if final_count:
                            raise ModelProviderError(
                                "model.provider_side_effect_unknown",
                                side_effect_state="unknown",
                            )
                        fragment = ModelStreamDelta(text=text)
                        next_size = self._observed_utf8_bytes + fragment.utf8_bytes
                        if next_size > MAX_STREAM_COLLECTOR_UTF8_BYTES:
                            raise ModelProviderError(
                                "model.provider_side_effect_unknown",
                                side_effect_state="unknown",
                            )
                        self._observed_fragments.append(text)
                        self._observed_utf8_bytes = next_size
                        yield fragment
            if final_count != 1 or self._result is None:
                raise ModelProviderError(
                    "model.provider_side_effect_unknown", side_effect_state="unknown"
                )
            if not isinstance(self._result.output, str):
                raise ModelProviderError(
                    "model.provider_side_effect_unknown", side_effect_state="unknown"
                )
            if self._result.output != "".join(self._observed_fragments):
                raise ModelProviderError(
                    "model.provider_side_effect_unknown", side_effect_state="unknown"
                )
            self._finished = True
        except TimeoutError:
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            ) from None
        finally:
            if self._finished:
                try:
                    await self._exit_context()
                except Exception:
                    # context 退出失败意味着远端停止状态不可证明；只暴露
                    # provider-neutral unknown，避免 SDK 异常和伪造 complete。
                    self._close_uncertain = True
                    raise ModelProviderError(
                        "model.provider_side_effect_unknown", side_effect_state="unknown"
                    ) from None

    async def result(self) -> ModelResponse:
        """自然耗尽且唯一 final 通过文本一致性校验后构造公开 DTO。"""

        if not self._finished or self._result is None:
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            )
        token_usage = self._read_token_usage()
        if token_usage is None:
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            ) from None
        cost = self.cost_estimator(self.plan, token_usage)
        attempt_tokens = (
            token_usage["input_tokens"] + token_usage["output_tokens"]
            if {"input_tokens", "output_tokens"}.issubset(token_usage)
            else None
        )
        attempt = ModelAttemptEvidence(
            attempt=1,
            side_effect_state="started",
            outcome="completed",
            completion_observed=True,
            input_tokens=token_usage.get("input_tokens"),
            output_tokens=token_usage.get("output_tokens"),
            cost_usd=cost,
            cost_status="estimated" if cost is not None else "unavailable",
            budget_charge_tokens=attempt_tokens,
            budget_charge_cost_usd=cost,
            latency_ms=int((perf_counter() - self.started_at) * 1000),
        )
        output = self._result.output
        if not isinstance(output, str):
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            )
        return ModelResponse(
            provider=self.provider_id,
            model=self.plan.model,
            output_text=output,
            decision=ModelDecision(
                action="call",
                estimated_tokens=self.plan.per_attempt_token_bound,
                price_source_ref=self.plan.price_source_ref,
                price_source_version=self.plan.price_source_version,
            ),
            token_usage=token_usage,
            latency_ms=int((perf_counter() - self.started_at) * 1000),
            cost_usd=cost,
            cost_status="estimated" if cost is not None else "unavailable",
            attempts=[attempt],
        )

    async def aclose(self) -> ModelStreamCloseResult:
        """取消活动迭代并幂等清理 context/permit，供组合根等待真实收口。"""

        current_task = asyncio.current_task()
        wait_for_close = False
        iteration_task: asyncio.Task[object] | None = None
        async with self._close_lock:
            if self._close_complete.is_set():
                return self._close_result()
            if self._close_owner_task is not None:
                # 外部组合根取消活动 pull 后，invocation 会在同一个迭代任务内
                # 回到 close seam 做 durable unknown 结算。该重入路径不能反向
                # 等待正在等待它结束的组合根，否则两者会形成生命周期死锁。
                if current_task is self._iteration_task:
                    return self._close_result()
                wait_for_close = True
            else:
                self._close_owner_task = current_task
                self._closed = True
                iteration_task = self._iteration_task
        if wait_for_close:
            await self._close_complete.wait()
            return self._close_result()
        try:
            if (
                iteration_task is not None
                and iteration_task is not current_task
                and not iteration_task.done()
            ):
                iteration_task.cancel()
                try:
                    await iteration_task
                except (Exception, asyncio.CancelledError):
                    # 调用任务负责把取消结算为稳定公开错误；provider 生命周期
                    # 这里只等待其完成，不能把业务结果重新泄漏给 composition close。
                    pass
            try:
                await self._exit_context()
            except Exception:
                # close 是事实分类 seam：清理失败必须降级为 unknown，不能把
                # SDK transport/context 异常泄漏给 invocation 或冒充已停止。
                self._close_uncertain = True
        finally:
            self.permit.release()
            try:
                if self.unregister is not None:
                    await self.unregister(self)
            finally:
                self._close_complete.set()
        return self._close_result()

    async def _exit_context(self) -> None:
        if self._context is None or self._context_exited:
            return
        self._context_exited = True
        await self._context.__aexit__(None, None, None)

    def _close_result(self) -> ModelStreamCloseResult:
        # `__aiter__` 只表示调用方请求第一次迭代；真正的 SDK/provider 边界从
        # context 创建开始。deadline 在此之前耗尽仍是可证明的 not-started。
        if self._context is None:
            return ModelStreamCloseResult(state="not_started")
        usage_payload = self._read_token_usage()
        if usage_payload is None:
            return ModelStreamCloseResult(state="unknown")
        cost = self.cost_estimator(self.plan, usage_payload)
        usage = ModelStreamUsage(
            finality=("complete" if self._finished and not self._close_uncertain else "partial"),
            input_tokens=usage_payload.get("input_tokens"),
            output_tokens=usage_payload.get("output_tokens"),
            cost_usd=cost,
            cost_status="estimated" if cost is not None else "unavailable",
            latency_ms=int((perf_counter() - self.started_at) * 1000),
        )
        if self._finished and not self._close_uncertain:
            return ModelStreamCloseResult(state="stopped", usage=usage)
        return ModelStreamCloseResult(state="unknown", usage=usage)

    def _read_token_usage(self) -> dict[str, int] | None:
        """只读取一次 SDK usage；非法形状降级 unknown，不从 close seam 逃逸。"""

        if self._usage_invalid:
            return None
        if self._usage_read:
            return self._usage_payload
        self._usage_read = True
        if self._result is None:
            return self._usage_payload
        try:
            self._usage_payload = self.token_usage_reader(self._result)
        except Exception:
            # SDK usage 是不可信 provider 输入；异常或非法数值只能丢弃计量并
            # 降级 unknown，使 invocation 仍能完成 durable needs-review 结算。
            self._usage_invalid = True
            self._close_uncertain = True
            return None
        return self._usage_payload


class PydanticStreamLifecycle:
    """统一拥有 prepare task、prepared stream 与 client factory 的关闭顺序。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._preparing_tasks: set[asyncio.Task[object]] = set()
        self._active_streams: dict[int, PreparedPydanticStream] = {}
        self._close_complete = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def owns_prepare(self, task: asyncio.Task[object] | None) -> bool:
        return task in self._preparing_tasks

    async def begin_prepare(self) -> asyncio.Task[object]:
        """在取得 permit/client 前登记调用任务，消除关闭快照盲区。"""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("stream prepare requires an asyncio task")
        async with self._lock:
            if self._closed:
                raise asyncio.CancelledError
            self._preparing_tasks.add(task)
        return task

    async def transfer(
        self,
        task: asyncio.Task[object],
        stream: PreparedPydanticStream,
    ) -> None:
        """原子完成 prepare task 到 active stream 的资源所有权转移。"""

        async with self._lock:
            if self._closed:
                raise asyncio.CancelledError
            self._active_streams[id(stream)] = stream
            self._preparing_tasks.discard(task)

    async def end_prepare(self, task: asyncio.Task[object]) -> None:
        """移除已取消或失败、未完成所有权转移的 prepare task。"""

        async with self._lock:
            self._preparing_tasks.discard(task)

    async def unregister(self, stream: PreparedPydanticStream) -> None:
        """移除已完全关闭的 prepared stream。"""

        async with self._lock:
            self._active_streams.pop(id(stream), None)

    async def aclose(self, close_client: ClientCloser) -> None:
        """依次收口 prepare、活动 stream 与共享 client，并支持并发幂等关闭。"""

        async with self._lock:
            if self._closed:
                wait_for_close = True
                preparing_tasks: tuple[asyncio.Task[object], ...] = ()
                streams: tuple[PreparedPydanticStream, ...] = ()
            else:
                self._closed = True
                wait_for_close = False
                preparing_tasks = tuple(self._preparing_tasks)
                streams = tuple(self._active_streams.values())
        if wait_for_close:
            await self._close_complete.wait()
            return
        try:
            current_task = asyncio.current_task()
            for task in preparing_tasks:
                if task is not current_task and not task.done():
                    task.cancel()
            for task in preparing_tasks:
                if task is current_task:
                    continue
                try:
                    await task
                except (Exception, asyncio.CancelledError):
                    # invocation task 自行把 prepare 取消耐久收口为 not-started；
                    # lifecycle 只等待该事实完成，不能泄漏其业务结果。
                    pass
            for stream in streams:
                await stream.aclose()
            await close_client()
        finally:
            self._close_complete.set()


__all__ = [
    "AgentRunResult",
    "PreparedPydanticStream",
    "PydanticStreamAgent",
    "PydanticStreamLifecycle",
    "StreamEventContext",
]
