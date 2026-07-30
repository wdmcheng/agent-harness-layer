"""离线 fake model provider。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from agent_harness.models.providers import (
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelStreamCloseResult,
    ModelStreamDelta,
    ModelStreamUsage,
)
from agent_harness.models.router import ModelRoutePlan


@dataclass(frozen=True)
class FakeModelStreamScript:
    """离线流的显式失败脚本，供 deadline、背压与未知关闭合同复现。"""

    fragments: tuple[str, ...]
    pause_gate: asyncio.Event | None = None
    pull_started: asyncio.Event | None = None
    fail_after_fragments: int | None = None
    close_result: ModelStreamCloseResult | None = None

    def __post_init__(self) -> None:
        """脚本必须保持有限且可解释，避免测试替身制造另一套流协议。"""

        if not self.fragments or any(not item for item in self.fragments):
            raise ValueError("fake stream script requires non-empty text fragments")
        if self.fail_after_fragments is not None and (
            isinstance(self.fail_after_fragments, bool)
            or not 0 <= self.fail_after_fragments < len(self.fragments)
        ):
            raise ValueError("fake stream failure ordinal must precede an existing fragment")


class FakeModelProvider:
    """测试和 local smoke 默认使用的确定性 provider。"""

    provider_id = "fake"

    def __init__(self, *, stream_script: FakeModelStreamScript | None = None) -> None:
        """可选脚本只改变离线 stream；默认 complete/local smoke 语义保持不变。"""

        self.stream_script = stream_script
        self.stream_pull_count = 0
        self.stream_close_count = 0

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """返回确定性文本，供 local smoke 和 contract tests 不依赖外部 provider。"""

        model = cast(ModelRoutePlan, plan).model
        output = f"fake:{request.prompt}"
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text=output,
            decision=ModelDecision(
                action="call",
                estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
            ),
            token_usage={
                "input_tokens": request.estimated_input_tokens,
                "output_tokens": min(request.max_output_tokens, len(output.split())),
            },
            latency_ms=0,
        )

    async def prepare_stream(self, request: ModelRequest, *, plan: object) -> _FakePreparedStream:
        """返回惰性离线流；prepare 阶段不形成任何 provider 观察。"""

        return _FakePreparedStream(
            provider=self,
            request=request,
            plan=cast(ModelRoutePlan, plan),
            # 既有测试/本地扩展可能在子类中覆盖 __init__ 而未调用 super；
            # 新增脚本能力保持可选，不能把这类历史 fake provider 变成启动失败。
            script=getattr(self, "stream_script", None),
        )

    async def aclose(self) -> None:
        """Fake 不持有外部资源；保留统一的 provider 生命周期协议。"""


class _FakePreparedStream:
    """Fake 的惰性流；默认单片段，显式脚本可稳定制造暂停与失败。"""

    def __init__(
        self,
        *,
        provider: FakeModelProvider,
        request: ModelRequest,
        plan: ModelRoutePlan,
        script: FakeModelStreamScript | None,
    ) -> None:
        self._provider = provider
        self._request = request
        self._plan = plan
        self._script = script
        self._started = False
        self._finished = False
        self._fragments = script.fragments if script is not None else (f"fake:{request.prompt}",)
        self._output = "".join(self._fragments)

    def __aiter__(self) -> AsyncIterator[ModelStreamDelta]:
        async def generate() -> AsyncIterator[ModelStreamDelta]:
            if self._started:
                raise RuntimeError("fake model stream can only be iterated once")
            self._started = True
            for emitted, fragment in enumerate(self._fragments):
                if self._script is not None and self._script.pull_started is not None:
                    self._script.pull_started.set()
                if self._script is not None and self._script.pause_gate is not None:
                    await self._script.pause_gate.wait()
                self._provider.stream_pull_count = (
                    getattr(self._provider, "stream_pull_count", 0) + 1
                )
                if self._script is not None and self._script.fail_after_fragments == emitted:
                    raise RuntimeError("fake scripted stream failure")
                yield ModelStreamDelta(text=fragment)
            self._finished = True

        return generate()

    async def result(self) -> ModelResponse:
        """只有自然耗尽后才提供唯一最终结果。"""

        if not self._finished:
            raise RuntimeError("fake model stream result is not ready")
        return ModelResponse(
            provider="fake",
            model=self._plan.model,
            output_text=self._output,
            decision=ModelDecision(
                action="call",
                estimated_tokens=(
                    self._request.estimated_input_tokens + self._request.max_output_tokens
                ),
            ),
            token_usage={
                "input_tokens": self._request.estimated_input_tokens,
                "output_tokens": min(
                    self._request.max_output_tokens,
                    len(self._output.split()),
                ),
            },
            latency_ms=0,
        )

    async def aclose(self) -> ModelStreamCloseResult:
        """未迭代与自然完成可精确分类；中途退出只报告 partial usage。"""

        self._provider.stream_close_count = getattr(self._provider, "stream_close_count", 0) + 1
        if self._script is not None and self._script.close_result is not None:
            return self._script.close_result
        if not self._started:
            return ModelStreamCloseResult(state="not_started")
        usage = ModelStreamUsage(
            finality="complete" if self._finished else "partial",
            input_tokens=self._request.estimated_input_tokens if self._finished else None,
            output_tokens=(
                min(self._request.max_output_tokens, len(self._output.split()))
                if self._finished
                else None
            ),
            cost_usd=None,
            cost_status="unavailable",
            latency_ms=0,
        )
        return ModelStreamCloseResult(state="stopped", usage=usage)


__all__ = ["FakeModelProvider", "FakeModelStreamScript"]
