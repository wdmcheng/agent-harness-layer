"""供应商中立文本流 public seam 合同。"""

from __future__ import annotations

import ast

# pyright: reportPrivateUsage=false
import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.model_streaming_sdk_event_test_helpers import (
    AgentRunResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    patch_pydantic_stream_event_types,
)
from tests.contracts.test_controlled_model_streaming_routing_contracts import (
    _policy,
    _request,
    _stream_settings,
)

import agent_harness.adapters.models._pydantic_ai_streaming as pydantic_ai_streaming
from agent_harness.adapters.models.pydantic_ai import ModelProviderError, PydanticAIModelProvider
from agent_harness.models import (
    FakeModelProvider,
    FakeModelStreamScript,
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    ModelStreamCloseResult,
    ModelStreamDelta,
    ModelStreamingProvider,
    ModelStreamUsage,
    PreparedModelStreamCall,
)
from agent_harness.models.streaming import MAX_STREAM_CHUNK_UTF8_BYTES, MAX_STREAM_DELTAS


class _PreparedStreamDouble:
    """只实现公开协议，证明合同不依赖任何供应商 SDK 类型。"""

    def __aiter__(self) -> AsyncIterator[ModelStreamDelta]:
        async def generate() -> AsyncIterator[ModelStreamDelta]:
            yield ModelStreamDelta(text="hello")

        return generate()

    async def result(self) -> ModelResponse:
        """返回供应商中立最终结果。"""

        return ModelResponse(
            provider="fake",
            model="fake-local",
            output_text="hello",
            decision=ModelDecision(action="call", estimated_tokens=2),
            token_usage={"input_tokens": 1, "output_tokens": 1},
        )

    async def aclose(self) -> ModelStreamCloseResult:
        """测试替身可证明流已停止并返回完整计量。"""

        return ModelStreamCloseResult(
            state="stopped",
            usage=ModelStreamUsage(
                finality="complete",
                input_tokens=1,
                output_tokens=1,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=0,
            ),
        )


@pytest.mark.asyncio
async def test_fake_stream_script_controls_pause_failure_and_close_fact() -> None:
    """默认离线 adapter 用显式脚本稳定制造慢消费、失败与 unknown/partial。"""

    gate = asyncio.Event()
    pull_started = asyncio.Event()
    script = FakeModelStreamScript(
        fragments=("first", "second"),
        pause_gate=gate,
        pull_started=pull_started,
        fail_after_fragments=1,
        close_result=ModelStreamCloseResult(
            state="unknown",
            usage=ModelStreamUsage(
                finality="partial",
                input_tokens=1,
                output_tokens=None,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=5,
            ),
        ),
    )
    provider = FakeModelProvider(stream_script=script)
    request = ModelRequest(capability="text_stream", prompt="scripted", max_output_tokens=8)
    router = ModelRouter(
        config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
        providers={"fake": provider},
    )
    prepared = await provider.prepare_stream(request, plan=router.plan(request))
    iterator = aiter(prepared)

    async def pull_first() -> ModelStreamDelta:
        """把通用 Awaitable 收窄为 create_task 所需的 coroutine。"""

        return await anext(iterator)

    first_pull = asyncio.create_task(pull_first())
    await asyncio.wait_for(pull_started.wait(), timeout=2)
    assert not first_pull.done()
    gate.set()
    assert await first_pull == ModelStreamDelta(text="first")
    with pytest.raises(RuntimeError, match="scripted stream failure"):
        await anext(iterator)

    assert await prepared.aclose() == script.close_result
    assert provider.stream_pull_count == 2
    assert provider.stream_close_count == 1


class _StreamingProviderDouble:
    """显式实现独立 prepare_stream，避免 complete 被切片伪装成流。"""

    provider_id = "fake"

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: object,
    ) -> PreparedModelStreamCall:
        del request, plan
        return _PreparedStreamDouble()


def test_stream_protocols_are_runtime_checkable_without_sdk_objects() -> None:
    """公开协议只检查稳定方法，不要求 Pydantic AI 事件或 client 类型。"""

    assert isinstance(_PreparedStreamDouble(), PreparedModelStreamCall)
    assert isinstance(_StreamingProviderDouble(), ModelStreamingProvider)


def test_stream_delta_only_accepts_non_empty_text_and_forbids_extra_fields() -> None:
    """provider delta 只能承载非空追加文本，ordinal 由 invocation 生成。"""

    assert ModelStreamDelta(text="增量").model_dump() == {"text": "增量"}
    with pytest.raises(ValidationError):
        ModelStreamDelta(text="")
    with pytest.raises(ValidationError):
        ModelStreamDelta.model_validate({"text": "ok", "cursor": "sdk-cursor"})


def test_stream_delta_rejects_single_fragment_before_fixed_collector_bound() -> None:
    """任意大 provider fragment 必须在进入 adapter/invocation collector 前失败。"""

    with pytest.raises(ValidationError, match="fixed collector bound"):
        ModelStreamDelta(text="x" * (MAX_STREAM_DELTAS * MAX_STREAM_CHUNK_UTF8_BYTES + 1))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", True),
        ("input_tokens", -1),
        ("output_tokens", "1"),
        ("latency_ms", False),
        ("latency_ms", -1),
        ("cost_usd", math.inf),
        ("cost_usd", -0.01),
    ],
)
def test_stream_usage_rejects_coercion_negative_and_non_finite_values(
    field: str,
    value: object,
) -> None:
    """部分计量也必须保持严格类型，未知值只能显式使用 null。"""

    payload: dict[str, object] = {
        "finality": "partial",
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 0,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        ModelStreamUsage.model_validate(payload)


@pytest.mark.parametrize(
    ("cost_usd", "cost_status"),
    [(0.0, "unavailable"), (None, "reported"), (None, "estimated")],
)
def test_stream_usage_cost_value_and_status_are_bijective(
    cost_usd: float | None,
    cost_status: str,
) -> None:
    """成本缺失当且仅当 unavailable，防止 null 被解释成已报告零成本。"""

    with pytest.raises(ValidationError):
        ModelStreamUsage.model_validate(
            {
                "finality": "partial",
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": cost_usd,
                "cost_status": cost_status,
                "latency_ms": 0,
            }
        )


@pytest.mark.parametrize(
    ("state", "finality", "accepted"),
    [
        ("not_started", None, True),
        ("not_started", "partial", False),
        ("stopped", None, True),
        ("stopped", "partial", True),
        ("stopped", "complete", True),
        ("unknown", None, True),
        ("unknown", "partial", True),
        ("unknown", "complete", False),
    ],
)
def test_stream_close_result_enforces_state_and_usage_matrix(
    state: str,
    finality: str | None,
    accepted: bool,
) -> None:
    """关闭 DTO 只表达已证明事实，unknown 不能携带伪完整计量。"""

    usage = (
        None
        if finality is None
        else ModelStreamUsage.model_validate(
            {
                "finality": finality,
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "cost_status": "unavailable",
                "latency_ms": 0,
            }
        )
    )
    payload = {"state": state, "usage": usage}

    if accepted:
        assert ModelStreamCloseResult.model_validate(payload).state == state
    else:
        with pytest.raises(ValidationError):
            ModelStreamCloseResult.model_validate(payload)


@dataclass
class _SDKResultDouble:
    output: str

    def usage(self) -> object:
        return SimpleNamespace(input_tokens=2, output_tokens=1)


class _SDKStreamAgentDouble:
    """按锁定 SDK event shape 产生文本、reasoning、part-end 与唯一 final。"""

    def __init__(self) -> None:
        self.iterations = 0
        self.exits = 0

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        assert prompt == "hello"
        assert model_settings is not None

        async def events():  # type: ignore[no-untyped-def]
            self.iterations += 1
            result = _SDKResultDouble(output="hello")
            yield PartStartEvent(index=0, part=TextPart(content="he"))
            yield PartStartEvent(index=1, part=ThinkingPart(content="private reasoning"))
            yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="llo"))
            yield PartEndEvent(index=0, part=TextPart(content="hello"))
            yield AgentRunResultEvent(result=cast(Any, result))

        try:
            yield events()
        finally:
            self.exits += 1


class _SDKStreamExitFailureAgentDouble:
    """模拟 SDK 已开始产出，但 context 退出阶段无法证明远端已停止。"""

    def __init__(self) -> None:
        self.exits = 0

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        assert prompt == "hello"
        assert model_settings is not None

        async def events():  # type: ignore[no-untyped-def]
            yield PartStartEvent(index=0, part=TextPart(content="he"))

        try:
            yield events()
        finally:
            self.exits += 1
            raise RuntimeError("sdk context exit failed")


class _SDKStreamEventSequenceAgentDouble:
    """按给定 SDK shape 序列验证缺失/重复 final 的关闭失败语义。"""

    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.exits = 0

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        assert prompt == "hello"
        assert model_settings is not None

        async def events():  # type: ignore[no-untyped-def]
            for event in self.events:
                yield event

        try:
            yield events()
        finally:
            self.exits += 1


def _pydantic_stream_provider(
    agent: object,
) -> tuple[PydanticAIModelProvider, ModelRouter, ModelRequest]:
    """构造固定流 deployment，避免 adapter 负路径重复装配无关配置。"""

    settings = _stream_settings()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    return provider, router, _request()


@pytest.mark.asyncio
async def test_pydantic_stream_filters_sdk_events_and_is_lazy_until_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_stream_events 只映射追加文本与唯一 final，prepare/context enter 均不迭代。"""

    patch_pydantic_stream_event_types(monkeypatch)
    agent = _SDKStreamAgentDouble()
    settings = _stream_settings()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    request = _request()
    plan = router.plan(request, agent_policy=_policy())

    prepared = await provider.prepare_stream(request, plan=plan)
    assert agent.iterations == 0
    deltas = [delta async for delta in prepared]
    result = await prepared.result()
    close_result = await prepared.aclose()

    assert deltas == [ModelStreamDelta(text="he"), ModelStreamDelta(text="llo")]
    assert result.output_text == "hello"
    assert result.token_usage == {"input_tokens": 2, "output_tokens": 1}
    assert len(result.attempts) == 1
    assert close_result.state == "stopped"
    assert close_result.usage is not None and close_result.usage.finality == "complete"
    assert agent.iterations == agent.exits == 1


def test_stream_fragment_accumulation_has_no_per_fragment_full_text_rebuild() -> None:
    """高碎片输入的累积工作必须线性，循环体不得反复 join 或执行字符串 `+=`。"""

    root = Path(__file__).resolve().parents[2]
    consumption_source = (
        root / "packages/agent-harness/src/agent_harness/models/_streaming_consumption.py"
    ).read_text(encoding="utf-8")
    adapter_source = (
        root / "packages/agent-harness/src/agent_harness/adapters/models/_pydantic_ai_streaming.py"
    ).read_text(encoding="utf-8")

    consumption_tree = ast.parse(consumption_source)
    loops = [node for node in ast.walk(consumption_tree) if isinstance(node, ast.AsyncFor)]
    assert loops
    assert all('"".join(raw_fragments)' not in ast.unparse(loop) for loop in loops)
    assert consumption_source.count('"".join(raw_fragments)') == 1

    adapter_tree = ast.parse(adapter_source)
    assert not any(
        isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Attribute)
        and node.target.attr == "_observed_text"
        for node in ast.walk(adapter_tree)
    )
    assert "_observed_fragments" in adapter_source


def test_streaming_capacity_repository_has_no_file_wide_type_suppression() -> None:
    """运行时输入必须用 ``object`` 局部收窄，不能以文件级规则掩盖类型矛盾。"""

    root = Path(__file__).resolve().parents[2]
    source = (
        root / "packages/agent-harness/src/agent_harness/storage/event_capacity_repositories.py"
    ).read_text(encoding="utf-8")

    assert "# pyright:" not in source


def test_pydantic_stream_agent_protocol_has_narrow_context_return_type() -> None:
    """SDK 惰性流入口必须返回窄 context protocol，不能把 ``Any`` 扩散到 adapter。"""

    root = Path(__file__).resolve().parents[2]
    source = (
        root / "packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    agent_protocol = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_PydanticAgent"
    )
    stream_method = next(
        node
        for node in agent_protocol.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_stream_events"
    )

    _assert_narrow_stream_context_return(stream_method)
    assert "StreamEventContext" in pydantic_ai_streaming.__all__
    assert "_sdk_stream_context_compatibility" in source


def test_pydantic_stream_agent_protocol_rejects_other_broad_return_types() -> None:
    """类型门禁必须拒绝 ``object`` 和宽泛容器，不能只对 ``Any`` 特判。"""

    for annotation in ("Any", "object", "dict[str, object]"):
        method = ast.parse(f"def run_stream_events() -> {annotation}: ...").body[0]
        assert isinstance(method, ast.FunctionDef)
        with pytest.raises(AssertionError):
            _assert_narrow_stream_context_return(method)


def _assert_narrow_stream_context_return(stream_method: ast.FunctionDef) -> None:
    """校验 Pydantic stream seam 精确绑定到可审查的窄 protocol。"""

    assert stream_method.returns is not None
    assert ast.unparse(stream_method.returns) == "StreamEventContext"


@pytest.mark.asyncio
async def test_pydantic_stream_close_normalizes_sdk_exit_failure_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK close 失败不得泄漏原始异常，也不得把停止状态冒充为已证明。"""

    patch_pydantic_stream_event_types(monkeypatch)
    agent = _SDKStreamExitFailureAgentDouble()
    settings = _stream_settings()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    request = _request()
    plan = router.plan(request, agent_policy=_policy())
    prepared = await provider.prepare_stream(request, plan=plan)

    async for delta in prepared:
        assert delta == ModelStreamDelta(text="he")
        break
    close_result = await prepared.aclose()

    assert close_result.state == "unknown"
    assert close_result.usage is not None and close_result.usage.finality == "partial"
    assert agent.exits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("final_count", [0, 2])
async def test_pydantic_stream_rejects_missing_or_duplicate_final(
    monkeypatch: pytest.MonkeyPatch,
    final_count: int,
) -> None:
    """自然耗尽必须恰有一个 final；缺失或重复均只暴露稳定 unknown。"""

    patch_pydantic_stream_event_types(monkeypatch)
    result = _SDKResultDouble(output="hello")
    events: list[object] = [PartStartEvent(index=0, part=TextPart(content="hello"))]
    events.extend(AgentRunResultEvent(result=result) for _ in range(final_count))
    agent = _SDKStreamEventSequenceAgentDouble(events)
    provider, router, request = _pydantic_stream_provider(agent)
    prepared = await provider.prepare_stream(
        request,
        plan=router.plan(request, agent_policy=_policy()),
    )

    with pytest.raises(ModelProviderError) as captured:
        _ = [delta async for delta in prepared]
    close_result = await prepared.aclose()

    assert captured.value.code == "model.provider_side_effect_unknown"
    assert captured.value.side_effect_state == "unknown"
    assert close_result.state == "unknown"
    assert close_result.usage is not None and close_result.usage.finality == "partial"
    assert agent.exits == 1
