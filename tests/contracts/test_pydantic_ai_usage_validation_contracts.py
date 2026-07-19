"""Pydantic AI token usage 的独立 nullable 与非法值拒绝合同。"""

from __future__ import annotations

import pytest

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.models import ModelRequest


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
    """返回预设 SDK 结果的同步 agent 替身，避免测试加载真实 provider。"""

    def __init__(self, result: _Result) -> None:
        """固定每次同步调用返回的结果对象，令用例仅覆盖 token 规范化。"""

        self._result = result

    def run_sync(self, prompt: str) -> _Result:
        """忽略 prompt 并返回预设结果，满足 Pydantic AI adapter 的最小调用协议。"""

        return self._result


def _normalized_usage(*, input_tokens: object, output_tokens: object) -> dict[str, int]:
    """通过真实 adapter 归一化模拟 SDK usage，集中复用 provider 装配细节。"""

    result = _Result(input_tokens=input_tokens, output_tokens=output_tokens)
    provider = PydanticAIModelProvider(agent_factory=lambda _: _Agent(result))
    return provider.complete(
        ModelRequest(provider="pydantic-ai", prompt="private"),
        model="planned-model",
    ).token_usage


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 2), (1, -2), (True, 2), (1.5, 2)],
)
def test_pydantic_ai_rejects_invalid_reported_token_values(
    input_tokens: object,
    output_tokens: object,
) -> None:
    """验证负数、布尔与浮点 token 值不能穿透 adapter 的 provider-neutral 边界。"""

    with pytest.raises(ValueError, match="token usage"):
        _normalized_usage(input_tokens=input_tokens, output_tokens=output_tokens)


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "expected"),
    [
        (7, None, {"input_tokens": 7}),
        (None, 4, {"output_tokens": 4}),
        (None, None, {}),
    ],
)
def test_pydantic_ai_preserves_each_known_token_value_independently(
    input_tokens: object,
    output_tokens: object,
    expected: dict[str, int],
) -> None:
    """验证 input/output token 可各自缺失，已知的一侧仍被精确保留。"""

    assert _normalized_usage(input_tokens=input_tokens, output_tokens=output_tokens) == expected
