"""Pydantic AI adapter 边界。

本模块是唯一允许 import `pydantic_ai` 的位置。业务 agent 和 router 只看
`ModelProvider` DTO；Pydantic AI 的 Agent/result 类型不穿过这个边界。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from time import perf_counter
from typing import Protocol, cast

from agent_harness.models.providers import ModelDecision, ModelRequest, ModelResponse


class _AgentRunResult(Protocol):
    output: object


class _PydanticAgent(Protocol):
    def run_sync(self, prompt: str) -> _AgentRunResult:
        """执行 Pydantic AI Agent 并返回 result。"""
        ...


AgentFactory = Callable[[str], _PydanticAgent]


class PydanticAIModelProvider:
    """把 Pydantic AI `Agent.run_sync()` 适配到 provider-neutral 接口。"""

    provider_id = "pydantic-ai"

    def __init__(
        self,
        *,
        instructions: str | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self._instructions = instructions
        self._agent_factory = agent_factory or self._default_agent_factory

    def _default_agent_factory(self, model: str) -> _PydanticAgent:
        from pydantic_ai import Agent

        if self._instructions is None:
            return cast(_PydanticAgent, Agent(model))
        return cast(_PydanticAgent, Agent(model, instructions=self._instructions))

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        started = perf_counter()
        agent = self._agent_factory(model)
        try:
            result = _run_sync_with_timeout(agent, request)
        except TimeoutError:
            return ModelResponse(
                provider=self.provider_id,
                model=model,
                output_text="",
                decision=ModelDecision(
                    action="policy_required",
                    estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
                    reason="pydantic-ai invocation timed out",
                ),
                token_usage={"input_tokens": request.estimated_input_tokens, "output_tokens": 0},
            )

        output_text = str(result.output)
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text=output_text,
            decision=ModelDecision(
                action="call",
                estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
            ),
            token_usage={
                "input_tokens": request.estimated_input_tokens,
                "output_tokens": min(request.max_output_tokens, len(output_text.split())),
            },
            latency_ms=int((perf_counter() - started) * 1000),
        )


def _run_sync_with_timeout(agent: _PydanticAgent, request: ModelRequest) -> _AgentRunResult:
    timeout = request.timeout_seconds
    if timeout is None or timeout <= 0:
        return agent.run_sync(request.prompt)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(agent.run_sync, request.prompt)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError("pydantic-ai invocation timed out") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
