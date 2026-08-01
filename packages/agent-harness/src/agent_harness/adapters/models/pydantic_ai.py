"""受控 Pydantic AI / OpenAI-compatible adapter 边界。

本 adapter 包是核心包唯一允许导入 `pydantic_ai`、`openai` 和其 HTTP client
对象的位置。业务 Agent、router、预算与 evidence 只消费 provider-neutral DTO。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic_ai.settings import ModelSettings as PydanticModelSettings

from agent_harness.adapters.models._pydantic_ai_client import (
    AgentFactory,
    ControlledOpenAIClientFactory,
    ControlledOpenAIClientLease,
    ControlledOpenAITransport,
    ModelProviderError,
)
from agent_harness.adapters.models._pydantic_ai_streaming import (
    AgentRunResult as _AgentRunResult,
)
from agent_harness.adapters.models._pydantic_ai_streaming import (
    PreparedPydanticStream,
    PydanticStreamLifecycle,
    StreamEventContext,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelDecision,
    ModelRequest,
    ModelResponse,
)
from agent_harness.models.router import ModelRoutePlan

if TYPE_CHECKING:
    from pydantic_ai import Agent as _SDKAgent

    # 该赋值只由 Pyright 分析，证明锁定 SDK 的真实返回类型兼容窄 protocol；
    # 不能用 Any 中转，否则 vendor 类型漂移会绕过 adapter 边界。
    _sdk_agent_for_typecheck = cast("_SDKAgent[None, str]", None)
    _sdk_stream_context_compatibility: StreamEventContext = (
        _sdk_agent_for_typecheck.run_stream_events("prompt", model_settings={})
    )


class _PydanticAgent(Protocol):
    """async Agent.run 最小 seam，便于真实 client 与离线 double 共用。"""

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object,
    ) -> _AgentRunResult:
        """执行单 user prompt、无 history/tools 的非流式调用。"""
        ...

    def run_stream_events(
        self,
        prompt: str,
        *,
        model_settings: object,
    ) -> StreamEventContext:
        """返回首次迭代才启动后台 run 的 SDK async context manager。"""
        ...


class PydanticAIModelProvider:
    """把 Pydantic AI async Agent.run 适配到 provider-neutral 非流式接口。"""

    def __init__(
        self,
        *,
        provider_id: str = "openai-compatible",
        client_factory: ControlledOpenAIClientFactory | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        if client_factory is None and agent_factory is None:
            raise ValueError("client_factory or agent_factory is required")
        self.provider_id = provider_id
        self._client_factory = client_factory
        self._agent_factory = agent_factory
        self._bulkheads: dict[str, asyncio.Semaphore] = {}
        self._stream_lifecycle = PydanticStreamLifecycle()

    async def aclose(self) -> None:
        """等待活动 stream/context 收口后，幂等关闭共享 client factory。"""

        async def close_client() -> None:
            if self._client_factory is not None:
                await self._client_factory.aclose()

        await self._stream_lifecycle.aclose(close_client)

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """兼容直接调用：prepare 后发送并确保释放 process-local permit。"""

        if not isinstance(plan, ModelRoutePlan):
            raise TypeError("controlled adapter requires ModelRoutePlan")
        prepared = await self.prepare(request, plan=plan)
        try:
            return await prepared.send()
        finally:
            await prepared.aclose()

    async def prepare(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> _PreparedPydanticCall:
        """在同一 total deadline 内取得 permit 与 lazy client，期间绝不发送请求。"""

        if self._stream_lifecycle.closed:
            current_task = asyncio.current_task()
            if self._stream_lifecycle.owns_prepare(current_task):
                raise asyncio.CancelledError
            raise RuntimeError("model provider is closed")
        if plan.provider != self.provider_id:
            raise ValueError("provider identity does not match frozen route")
        loop = asyncio.get_running_loop()
        started_at = perf_counter()
        deadline = loop.time() + plan.total_timeout_ms / 1000
        maximum = plan.bulkhead_policy.max_in_flight
        semaphore = self._bulkheads.setdefault(plan.deployment_id, asyncio.Semaphore(maximum))
        queue_timeout = plan.bulkhead_policy.queue_timeout_ms / 1000
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise ModelProviderError("model.invocation_cancelled", side_effect_state="not_started")
        deadline_limits_queue = remaining <= queue_timeout
        try:
            async with asyncio.timeout(min(queue_timeout, remaining)):
                await semaphore.acquire()
        except TimeoutError:
            raise ModelProviderError(
                (
                    "model.invocation_cancelled"
                    if deadline_limits_queue
                    else "model.bulkhead_saturated"
                ),
                side_effect_state="not_started",
            ) from None
        try:
            if self._agent_factory is not None:
                agent = cast(_PydanticAgent, self._agent_factory(plan))
            else:
                assert self._client_factory is not None
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    lease = await self._client_factory.acquire(plan)
                agent = cast(_PydanticAgent, lease.agent)
            if loop.time() >= deadline:
                raise TimeoutError
        except TimeoutError:
            semaphore.release()
            raise ModelProviderError(
                "model.invocation_cancelled", side_effect_state="not_started"
            ) from None
        except ModelProviderError:
            semaphore.release()
            raise
        except Exception:
            semaphore.release()
            # lazy factory/agent 构造发生在任何 send/iterate 之前；只向核心暴露
            # 封闭的 client-not-started 事实，不能让 vendor 或配置异常被误记为
            # 已发送 unknown。显式取消仍由 BaseException 分支原样传播。
            raise ModelProviderError(
                "model.provider_failed",
                completion_observed=False,
                side_effect_state="not_started",
            ) from None
        except BaseException:
            semaphore.release()
            raise
        return _PreparedPydanticCall(
            provider=self,
            request=request,
            plan=plan,
            agent=agent,
            permit=semaphore,
            deadline=deadline,
            started_at=started_at,
        )

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedPydanticStream:
        """取得与非流式相同的 permit/client，但把所有权转交惰性 event stream。"""

        prepare_task = await self._stream_lifecycle.begin_prepare()
        prepared: _PreparedPydanticCall | None = None
        stream: PreparedPydanticStream | None = None
        registered = False
        try:
            prepared = await self.prepare(request, plan=plan)
            # `_PreparedPydanticCall` 只是资源取得载体；这里显式转移 permit
            # 所有权，防止两个 prepared 对象重复 release 同一 semaphore。
            prepared.transfer_to_stream()
            stream = PreparedPydanticStream(
                provider_id=self.provider_id,
                request=request,
                plan=plan,
                agent=prepared.agent,
                permit=prepared.permit,
                deadline=prepared.deadline,
                started_at=prepared.started_at,
                token_usage_reader=_provider_token_usage,
                cost_estimator=_estimated_cost,
                unregister=self._stream_lifecycle.unregister,
            )
            # prepare task → active stream 的所有权转移与 provider close
            # 共用一把锁，不能留下快照看不见两者的生命周期空窗。
            await self._stream_lifecycle.transfer(prepare_task, stream)
            registered = True
            return stream
        except BaseException:
            if stream is not None:
                await stream.aclose()
            elif prepared is not None:
                await prepared.aclose()
            raise
        finally:
            if not registered:
                await self._stream_lifecycle.end_prepare(prepare_task)

    async def execute_prepared(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        agent: _PydanticAgent,
        deadline: float,
        started_at: float,
    ) -> ModelResponse:
        """在 durable mark 之后执行唯一 send/retry 控制器并归一化证据。"""

        loop = asyncio.get_running_loop()
        attempts: list[ModelAttemptEvidence] = []
        result: _AgentRunResult | None = None
        for attempt_number in range(1, plan.max_attempts + 1):
            attempt_started = perf_counter()
            remaining = deadline - loop.time()
            if remaining <= 0:
                attempts.append(
                    ModelAttemptEvidence(
                        attempt=attempt_number,
                        side_effect_state="unknown",
                        outcome="unknown",
                        latency_ms=0,
                        error_code="model.provider_side_effect_unknown",
                    )
                )
                raise ModelProviderError(
                    "model.provider_side_effect_unknown",
                    side_effect_state="unknown",
                    attempts=tuple(attempts),
                )
            try:
                async with asyncio.timeout(remaining):
                    result = await agent.run(
                        request.prompt,
                        model_settings=PydanticModelSettings(max_tokens=plan.output_token_cap),
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                attempts.append(
                    ModelAttemptEvidence(
                        attempt=attempt_number,
                        side_effect_state="unknown",
                        outcome="failed",
                        completion_observed=None,
                        latency_ms=int((perf_counter() - attempt_started) * 1000),
                        error_code="model.provider_side_effect_unknown",
                    )
                )
                raise ModelProviderError(
                    "model.provider_side_effect_unknown", attempts=tuple(attempts)
                ) from None
            except ModelProviderError as exc:
                side_effect_state = (
                    exc.side_effect_state
                    if exc.side_effect_state in {"not_started", "started", "unknown"}
                    else "unknown"
                )
                attempts.append(
                    ModelAttemptEvidence(
                        attempt=attempt_number,
                        side_effect_state=cast(Any, side_effect_state),
                        outcome=(
                            "retryable_status"
                            if (
                                exc.status_code in plan.retry_policy.retryable_http_statuses
                                or (
                                    exc.status_code is None
                                    and exc.side_effect_state == "not_started"
                                    and exc.completion_observed is False
                                )
                            )
                            else "failed"
                        ),
                        completion_observed=exc.completion_observed,
                        http_status=exc.status_code,
                        retry_after_ms=exc.retry_after_ms,
                        latency_ms=int((perf_counter() - attempt_started) * 1000),
                        cost_status="unavailable",
                        budget_charge_tokens=(0 if side_effect_state == "not_started" else None),
                        budget_charge_cost_usd=(
                            0.0
                            if side_effect_state == "not_started"
                            and plan.input_token_price_usd is not None
                            else None
                        ),
                        error_code=exc.code,
                    )
                )
                trusted_classifier = (
                    plan.completion_classifier_ref == "trusted_response_header_not_started"
                    and plan.completion_classifier_version == "v1"
                )
                retryable_transport = (
                    exc.status_code is None
                    and exc.side_effect_state == "not_started"
                    and exc.completion_observed is False
                )
                retryable = retryable_transport or (
                    trusted_classifier
                    and exc.completion_observed is False
                    and exc.status_code in plan.retry_policy.retryable_http_statuses
                )
                if not retryable:
                    exc.attempts = tuple(attempts)
                    raise
                if attempt_number >= plan.max_attempts:
                    raise ModelProviderError(
                        "model.provider_retry_exhausted",
                        status_code=exc.status_code,
                        retry_after_ms=exc.retry_after_ms,
                        completion_observed=exc.completion_observed,
                        side_effect_state=exc.side_effect_state,
                        attempts=tuple(attempts),
                    ) from None
                retry_after_ms = max(0, exc.retry_after_ms or 0)
                initial_backoff_ms = plan.retry_policy.backoff_initial_ms
                backoff_ms = min(
                    initial_backoff_ms * (2 ** (attempt_number - 1)),
                    plan.retry_policy.backoff_max_ms,
                )
                wait_ms = min(
                    max(retry_after_ms, backoff_ms),
                    plan.retry_policy.max_wait_ms,
                )
                if loop.time() + wait_ms / 1000 >= deadline:
                    raise ModelProviderError(
                        "model.provider_retry_exhausted",
                        status_code=exc.status_code,
                        retry_after_ms=exc.retry_after_ms,
                        completion_observed=exc.completion_observed,
                        side_effect_state=exc.side_effect_state,
                        attempts=tuple(attempts),
                    ) from None
                if wait_ms:
                    await asyncio.sleep(wait_ms / 1000)
                continue
            token_usage = _provider_token_usage(result)
            attempt_cost = _estimated_cost(plan, token_usage)
            attempt_tokens = (
                token_usage["input_tokens"] + token_usage["output_tokens"]
                if {"input_tokens", "output_tokens"}.issubset(token_usage)
                else None
            )
            attempts.append(
                ModelAttemptEvidence(
                    attempt=attempt_number,
                    side_effect_state="started",
                    outcome="completed",
                    completion_observed=True,
                    latency_ms=int((perf_counter() - attempt_started) * 1000),
                    input_tokens=token_usage.get("input_tokens"),
                    output_tokens=token_usage.get("output_tokens"),
                    cost_usd=attempt_cost,
                    cost_status="estimated" if attempt_cost is not None else "unavailable",
                    budget_charge_tokens=attempt_tokens,
                    budget_charge_cost_usd=attempt_cost,
                )
            )
            break
        if result is None:
            raise ModelProviderError("model.provider_retry_exhausted", attempts=tuple(attempts))
        billable_attempts = [
            item for item in attempts if item.side_effect_state in {"started", "unknown"}
        ]
        token_usage = (
            {
                "input_tokens": sum(item.input_tokens or 0 for item in billable_attempts),
                "output_tokens": sum(item.output_tokens or 0 for item in billable_attempts),
            }
            if all(
                item.side_effect_state == "started"
                and item.input_tokens is not None
                and item.output_tokens is not None
                for item in billable_attempts
            )
            else {}
        )
        total_cost = (
            sum((item.cost_usd or 0.0 for item in billable_attempts), 0.0)
            if billable_attempts
            and all(
                item.side_effect_state == "started" and item.cost_usd is not None
                for item in billable_attempts
            )
            else None
        )
        return ModelResponse(
            provider=self.provider_id,
            model=plan.model,
            output_text=str(result.output),
            decision=ModelDecision(
                action="call",
                estimated_tokens=plan.per_attempt_token_bound,
                price_source_ref=plan.price_source_ref,
                price_source_version=plan.price_source_version,
            ),
            token_usage=token_usage,
            latency_ms=int((perf_counter() - started_at) * 1000),
            cost_usd=total_cost,
            cost_status="estimated" if total_cost is not None else "unavailable",
            attempts=attempts,
        )


@dataclass
class _PreparedPydanticCall:
    """持有单次调用 permit 和已验证 client/Agent；构造阶段不触网。"""

    provider: PydanticAIModelProvider
    request: ModelRequest
    plan: ModelRoutePlan
    agent: _PydanticAgent
    permit: asyncio.Semaphore
    deadline: float
    started_at: float
    _closed: bool = False
    _sent: bool = False

    def transfer_to_stream(self) -> None:
        """把 permit 所有权转交 stream 对象，禁止 call wrapper 再次释放。"""

        if self._closed or self._sent:
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            )
        self._closed = True

    async def send(self) -> ModelResponse:
        """只允许发送一次，避免同一 durable mark 被进程内重复消费。"""

        if self._closed or self._sent:
            raise ModelProviderError(
                "model.provider_side_effect_unknown", side_effect_state="unknown"
            )
        self._sent = True
        return await self.provider.execute_prepared(
            self.request,
            plan=self.plan,
            agent=self.agent,
            deadline=self.deadline,
            started_at=self.started_at,
        )

    async def aclose(self) -> None:
        """幂等释放 permit；缓存 client 仍由 composition root 关闭。"""

        if self._closed:
            return
        self._closed = True
        self.permit.release()


def _provider_token_usage(result: _AgentRunResult) -> dict[str, int]:
    """只收敛 provider 明确报告的合法 token；缺失时保持 unknown。"""

    usage_reader = getattr(result, "usage", None)
    if not callable(usage_reader):
        return {}
    usage = usage_reader()
    normalized: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens"):
        value = getattr(usage, field, None)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("provider token usage must be a non-negative integer or null")
        normalized[field] = value
    return normalized


def _estimated_cost(plan: ModelRoutePlan, usage: dict[str, int]) -> float | None:
    """仅在两维 actual usage 与冻结价格齐全时计算精确 Decimal 成本。"""

    if (
        plan.input_token_price_usd is None
        or plan.output_token_price_usd is None
        or "input_tokens" not in usage
        or "output_tokens" not in usage
    ):
        return None
    value = (
        plan.input_token_price_usd * usage["input_tokens"]
        + plan.output_token_price_usd * usage["output_tokens"]
    )
    return float(value)


__all__ = [
    "ControlledOpenAIClientFactory",
    "ControlledOpenAIClientLease",
    "ControlledOpenAITransport",
    "ModelProviderError",
    "PydanticAIModelProvider",
]
