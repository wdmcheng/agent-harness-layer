"""离线 fake model provider。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelStreamCloseResult,
    ModelStreamDelta,
    ModelStreamUsage,
    StructuredProviderCandidate,
)
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    canonical_structured_json,
)

if TYPE_CHECKING:
    from agent_harness.models.tool_intent import ProviderToolIntentCandidate


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


@dataclass(frozen=True)
class FakeStructuredScript:
    """离线 structured candidates 的显式有限脚本，不作为真实 provider 后备。"""

    candidates: tuple[str | dict[str, Any], ...]

    def __post_init__(self) -> None:
        """脚本必须至少覆盖一次 send，并保持普通 JSON-compatible 值。"""

        if not self.candidates:
            raise ValueError("fake structured script requires at least one candidate")
        for candidate in self.candidates:
            if isinstance(candidate, dict):
                canonical_structured_json(candidate)


@dataclass(frozen=True)
class FakeToolIntentScript:
    """离线 final/tool 结果的显式有限脚本，不注册任何 executable callback。"""

    results: tuple[ModelResponse | ProviderToolIntentCandidate, ...]

    def __post_init__(self) -> None:
        """只接受 exact provider-neutral DTO，避免测试脚本夹带 SDK 或 callable。"""

        from agent_harness.models.tool_intent import ProviderToolIntentCandidate

        if not self.results or any(
            type(result) not in {ModelResponse, ProviderToolIntentCandidate}
            for result in self.results
        ):
            raise ValueError("fake tool-intent script requires exact provider-neutral results")


class FakeModelProvider:
    """测试和 local smoke 默认使用的确定性 provider。"""

    provider_id = "fake"

    def __init__(
        self,
        *,
        stream_script: FakeModelStreamScript | None = None,
        structured_script: FakeStructuredScript | None = None,
        tool_intent_script: FakeToolIntentScript | None = None,
    ) -> None:
        """可选脚本只改变离线 stream；默认 complete/local smoke 语义保持不变。"""

        self.stream_script = stream_script
        self.stream_pull_count = 0
        self.stream_close_count = 0
        self.structured_script = structured_script
        self.structured_send_count = 0
        self.structured_close_count = 0
        self.tool_intent_script = tool_intent_script
        self.tool_intent_prepare_count = 0
        self.tool_intent_send_count = 0
        self.tool_intent_close_count = 0
        self.provider_native_tool_execution_count = 0

    @property
    def tool_intent_observation_supported(self) -> bool:
        """只有显式脚本存在时才宣告离线 proposal observation capability。"""

        return self.tool_intent_script is not None

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

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: object,
        schema: OutputSchemaDefinition,
    ) -> _FakePreparedStructuredCall:
        """创建一次性离线 handle；prepare 本身不推进脚本或计数。"""

        return _FakePreparedStructuredCall(
            provider=self,
            request=request,
            plan=cast(ModelRoutePlan, plan),
            schema=schema,
        )

    async def prepare_tool_intent(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        tool_catalog_json: bytes,
    ) -> _FakePreparedToolIntentCall:
        """只接受 route 冻结的 exact catalog bytes；prepare 不消费脚本。"""

        if (
            request.capability != "tool_intent"
            or plan.capability != "tool_intent"
            or plan.provider != self.provider_id
            or plan.model != request.model
            or plan.provider_tool_catalog_json is None
            or tool_catalog_json != plan.provider_tool_catalog_json.encode("utf-8")
        ):
            raise ValueError("fake tool-intent request does not match frozen route")
        script = self.tool_intent_script
        if script is None:
            raise ValueError("fake tool-intent requires an explicit script")
        self.tool_intent_prepare_count += 1
        return _FakePreparedToolIntentCall(provider=self, script=script)

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


class _FakePreparedStructuredCall:
    """Fake structured 的一次性 prepared handle。"""

    def __init__(
        self,
        *,
        provider: FakeModelProvider,
        request: ModelRequest,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> None:
        self._provider = provider
        self._request = request
        self._plan = plan
        self._schema = schema
        self._sent = False
        self._closed = False

    @staticmethod
    def _default_value(schema: dict[str, Any]) -> object:
        """仅为 local smoke 构造最小确定值；核心 validator 仍是最终 oracle。"""

        if "const" in schema:
            return schema["const"]
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return cast(list[object], enum)[0]
        schema_type = schema.get("type")
        if schema_type == "object":
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if not isinstance(properties, dict) or not isinstance(required, list):
                return {}
            typed_properties = cast(dict[str, object], properties)
            typed_required = cast(list[object], required)
            return {
                str(name): _FakePreparedStructuredCall._default_value(
                    cast(dict[str, Any], typed_properties[str(name)])
                )
                for name in typed_required
                if str(name) in typed_properties and isinstance(typed_properties[str(name)], dict)
            }
        if schema_type == "array":
            return []
        if schema_type == "string":
            return "fake"
        if schema_type == "integer":
            return 0
        if schema_type == "number":
            return 0.0
        if schema_type == "boolean":
            return False
        any_of = schema.get("anyOf")
        if isinstance(any_of, list) and any_of:
            first = cast(list[object], any_of)[0]
            if isinstance(first, dict):
                return _FakePreparedStructuredCall._default_value(cast(dict[str, Any], first))
        return None

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """每次 handle 恰好贡献一个 local attempt，不在 fake 内 repair/retry。"""

        del repair_ordinal, transport_ordinal
        if self._sent or self._closed:
            raise RuntimeError("fake structured handle can only send once")
        self._sent = True
        index = self._provider.structured_send_count
        self._provider.structured_send_count += 1
        script = getattr(self._provider, "structured_script", None)
        if script is not None:
            candidate = script.candidates[min(index, len(script.candidates) - 1)]
        else:
            candidate = cast(dict[str, Any], self._default_value(self._schema.schema_definition))
        output_text = (
            candidate if isinstance(candidate, str) else canonical_structured_json(candidate)
        )
        input_tokens = len(provider_prompt.encode("utf-8"))
        output_tokens = min(
            self._plan.output_token_cap,
            len(output_text.encode("utf-8")),
        )
        cost_usd = (
            None
            if self._plan.input_token_price_usd is None or self._plan.output_token_price_usd is None
            else float(
                self._plan.input_token_price_usd * input_tokens
                + self._plan.output_token_price_usd * output_tokens
            )
        )
        return StructuredProviderCandidate(
            schema_identity=self._schema.identity,
            provider=self._provider.provider_id,
            model=self._plan.model,
            candidate=candidate,
            attempts=[
                ModelAttemptEvidence(
                    attempt=1,
                    side_effect_state="started",
                    outcome="completed",
                    completion_observed=True,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    cost_status="estimated" if cost_usd is not None else "unavailable",
                    latency_ms=0,
                )
            ],
        )

    async def aclose(self) -> None:
        """计数并拒绝重复 cleanup，帮助合同发现核心生命周期错误。"""

        if self._closed:
            raise RuntimeError("fake structured handle was closed more than once")
        self._closed = True
        self._provider.structured_close_count += 1


class _FakePreparedToolIntentCall:
    """Fake tool-intent 的一次性 handle；只观察脚本 DTO，不接触工具执行层。"""

    def __init__(self, *, provider: FakeModelProvider, script: FakeToolIntentScript) -> None:
        self._provider = provider
        self._script = script
        self._sent = False
        self._closed = False

    async def send_tool_intent(self) -> ModelResponse | ProviderToolIntentCandidate:
        """每个 handle 恰好返回一个显式结果，不 retry、不执行 provider-native tool。"""

        if self._sent or self._closed:
            raise RuntimeError("fake tool-intent handle can only send once")
        self._sent = True
        index = self._provider.tool_intent_send_count
        self._provider.tool_intent_send_count += 1
        return self._script.results[min(index, len(self._script.results) - 1)]

    async def aclose(self) -> None:
        """计数并拒绝重复 cleanup，暴露核心生命周期漂移。"""

        if self._closed:
            raise RuntimeError("fake tool-intent handle was closed more than once")
        self._closed = True
        self._provider.tool_intent_close_count += 1


__all__ = [
    "FakeModelProvider",
    "FakeModelStreamScript",
    "FakeStructuredScript",
    "FakeToolIntentScript",
]
