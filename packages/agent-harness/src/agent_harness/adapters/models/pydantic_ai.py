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
    """隔离 Pydantic AI result 所需的最小表面，防止 SDK 类型进入核心模型层。"""

    output: object

    def usage(self) -> object:
        """返回 provider SDK usage；只允许本 adapter 读取。"""
        ...


class _PydanticAgent(Protocol):
    """隔离同步 Agent 调用面，便于测试替身与可选依赖延迟加载。"""

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
        """保存可选 instructions 与工厂 seam；默认工厂只在真实调用时导入 SDK。"""

        self._instructions = instructions
        self._agent_factory = agent_factory or self._default_agent_factory

    def _default_agent_factory(self, model: str) -> _PydanticAgent:
        """按路由模型创建 SDK Agent，并仅在配置存在时传入静态 instructions。"""

        from pydantic_ai import Agent

        if self._instructions is None:
            return cast(_PydanticAgent, Agent(model))
        return cast(_PydanticAgent, Agent(model, instructions=self._instructions))

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        """执行 Pydantic AI agent，并把 timeout/usage 收敛成内部 ModelResponse。"""

        started = perf_counter()
        agent = self._agent_factory(model)
        result = _run_sync_with_timeout(agent, request)

        output_text = str(result.output)
        token_usage = _provider_token_usage(result)
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text=output_text,
            decision=ModelDecision(
                action="call",
                estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
            ),
            token_usage=token_usage,
            latency_ms=int((perf_counter() - started) * 1000),
        )


def _run_sync_with_timeout(agent: _PydanticAgent, request: ModelRequest) -> _AgentRunResult:
    """在同步 Pydantic AI 调用外包一层超时边界。

    Pydantic AI 的同步 API 没有被暴露到 router；这里用线程池把 timeout
    限制留在 adapter 内，失败时上层只看到 provider-neutral decision。
    """

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


def _provider_token_usage(
    result: _AgentRunResult,
) -> dict[str, int]:
    """只收敛 provider 明确报告的合法 token；缺失时保持 unknown。"""

    usage_reader = getattr(result, "usage", None)
    if callable(usage_reader):
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
    return {}
