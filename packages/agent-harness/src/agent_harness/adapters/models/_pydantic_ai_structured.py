"""Pydantic AI 结构化单次发送与候选归一化边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, cast

from pydantic_ai.settings import ModelSettings as PydanticModelSettings

from agent_harness.adapters.models._pydantic_ai_streaming import (
    AgentRunResult,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    StructuredProviderCallError,
    StructuredProviderCandidate,
)
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    canonical_structured_json,
)


class _StructuredAgent(Protocol):
    """只暴露结构化适配所需的单次无工具调用。"""

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object,
        retries: int | None = None,
    ) -> AgentRunResult: ...


class _ProviderIdentity(Protocol):
    """prepared handle只读取冻结provider identity。"""

    @property
    def provider_id(self) -> str: ...


class _PreparedCall(Protocol):
    """隔离结构化发送对通用Pydantic prepared资源的最小依赖。"""

    @property
    def provider(self) -> _ProviderIdentity: ...

    @property
    def plan(self) -> ModelRoutePlan: ...

    @property
    def agent(self) -> _StructuredAgent: ...

    @property
    def deadline(self) -> float: ...

    async def aclose(self) -> None: ...


TokenUsageReader = Callable[[AgentRunResult], dict[str, int]]
CostEstimator = Callable[[ModelRoutePlan, dict[str, int]], float | None]


@dataclass
class PreparedPydanticStructuredCall:
    """把一次Pydantic AI调用限定为一个Harness结构化请求。"""

    prepared: _PreparedCall
    schema: OutputSchemaDefinition
    token_usage_reader: TokenUsageReader
    cost_estimator: CostEstimator
    _sent: bool = False
    _closed: bool = False

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """禁用SDK retry；adapter只归一化候选，业务有效性留给核心validator。"""

        del repair_ordinal, transport_ordinal
        if self._sent or self._closed:
            raise StructuredProviderCallError(
                code="model.provider_side_effect_unknown",
                attempts=[
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="unknown",
                        outcome="unknown",
                        latency_ms=0,
                        error_code="model.provider_side_effect_unknown",
                    )
                ],
            )
        self._sent = True
        loop = asyncio.get_running_loop()
        attempt_started = perf_counter()
        remaining = self.prepared.deadline - loop.time()
        if remaining <= 0:
            raise StructuredProviderCallError(
                code="model.provider_side_effect_unknown",
                attempts=[
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="unknown",
                        outcome="unknown",
                        latency_ms=0,
                        error_code="model.provider_side_effect_unknown",
                    )
                ],
            )
        try:
            async with asyncio.timeout(remaining):
                result = await self.prepared.agent.run(
                    provider_prompt,
                    model_settings=PydanticModelSettings(
                        max_tokens=self.prepared.plan.output_token_cap
                    ),
                    retries=0,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise StructuredProviderCallError(
                code="model.provider_side_effect_unknown",
                attempts=[
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="unknown",
                        outcome="unknown",
                        latency_ms=int((perf_counter() - attempt_started) * 1000),
                        error_code="model.provider_side_effect_unknown",
                    )
                ],
            ) from None
        usage = self.token_usage_reader(result)
        attempt_cost = self.cost_estimator(self.prepared.plan, usage)
        output = result.output
        if isinstance(output, str):
            candidate: str | dict[str, Any] = output
        elif isinstance(output, dict):
            candidate = cast(dict[str, Any], output)
            canonical_structured_json(candidate)
        else:
            raise StructuredProviderCallError(
                code="model.provider_failed",
                attempts=[
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="started",
                        outcome="failed",
                        completion_observed=True,
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        cost_usd=attempt_cost,
                        cost_status=("estimated" if attempt_cost is not None else "unavailable"),
                        latency_ms=int((perf_counter() - attempt_started) * 1000),
                        error_code="model.provider_failed",
                    )
                ],
            )
        return StructuredProviderCandidate(
            schema_identity=self.schema.identity,
            provider=self.prepared.provider.provider_id,
            model=self.prepared.plan.model,
            candidate=candidate,
            attempts=[
                ModelAttemptEvidence(
                    attempt=1,
                    side_effect_state="started",
                    outcome="completed",
                    completion_observed=True,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cost_usd=attempt_cost,
                    cost_status=("estimated" if attempt_cost is not None else "unavailable"),
                    latency_ms=int((perf_counter() - attempt_started) * 1000),
                )
            ],
        )

    async def aclose(self) -> None:
        """由核心恰调用一次；底层permit release保持幂等。"""

        if self._closed:
            raise RuntimeError("structured Pydantic handle was closed more than once")
        self._closed = True
        await self.prepared.aclose()


__all__ = ["PreparedPydanticStructuredCall"]
