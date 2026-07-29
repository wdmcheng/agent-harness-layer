"""Pydantic AI token usage 的独立 nullable 与非法值拒绝合同。"""

from __future__ import annotations

import pytest

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.models import ModelDecision, ModelRequest, ModelResponse, ModelRoutePlan


class _Result:
    """模拟 Pydantic AI 返回对象，只暴露 adapter 读取的 output/usage 形状。"""

    output: object = "unused"

    def __init__(self, *, input_tokens: object, output_tokens: object) -> None:
        """将任意测试 token 值放入匿名 usage 对象，以覆盖 adapter 的后置校验。"""

        self._usage = type(
            "Usage",
            (),
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
        )()

    def usage(self) -> object:
        """按 SDK 风格返回 usage 对象，不预先执行类型转换或范围校验。"""

        return self._usage


class _Agent:
    """返回预设 SDK 结果的异步 agent 替身，避免测试加载真实 provider。"""

    def __init__(self, result: _Result) -> None:
        """固定每次同步调用返回的结果对象，令用例仅覆盖 token 规范化。"""

        self._result = result

    async def run(self, prompt: str, *, model_settings: object) -> _Result:
        """忽略 prompt/cap 并返回预设结果，满足 async adapter 最小协议。"""

        del prompt, model_settings
        return self._result


async def _normalized_response(*, input_tokens: object, output_tokens: object) -> ModelResponse:
    """通过真实 adapter 归一化模拟 SDK response，集中复用 provider 装配细节。"""

    result = _Result(input_tokens=input_tokens, output_tokens=output_tokens)
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _: _Agent(result),
    )
    plan = ModelRoutePlan(
        deployment_id="fixture",
        provider_kind="openai-compatible",
        provider="openai-compatible",
        model="planned-model",
        decision=ModelDecision(action="call", estimated_tokens=1),
        output_token_cap=1,
        per_attempt_token_bound=1,
        trusted_token_bound=1,
        trusted_cost_bound=None,
        total_timeout_ms=1000,
    )
    return await provider.complete(
        ModelRequest(provider="openai-compatible", prompt="private", max_output_tokens=1),
        plan=plan,
    )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 2), (1, -2), (True, 2), (1.5, 2)],
)
@pytest.mark.asyncio
async def test_pydantic_ai_rejects_invalid_reported_token_values(
    input_tokens: object,
    output_tokens: object,
) -> None:
    """验证负数、布尔与浮点 token 值不能穿透 adapter 的 provider-neutral 边界。"""

    with pytest.raises(ValueError, match="token usage"):
        await _normalized_response(input_tokens=input_tokens, output_tokens=output_tokens)


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [
        (7, None),
        (None, 4),
        (None, None),
    ],
)
@pytest.mark.asyncio
async def test_pydantic_ai_keeps_partial_usage_only_in_attempt_evidence(
    input_tokens: object,
    output_tokens: object,
) -> None:
    """单边 actual 可留在 attempt，但不得伪装成完整响应级 token 聚合。"""

    response = await _normalized_response(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    assert response.token_usage == {}
    assert len(response.attempts) == 1
    assert response.attempts[0].input_tokens == input_tokens
    assert response.attempts[0].output_tokens == output_tokens
