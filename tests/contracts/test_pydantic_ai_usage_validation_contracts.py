"""Pydantic AI token usage 的独立 nullable 与非法值拒绝合同。"""

from __future__ import annotations

import pytest

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.models import ModelRequest


class _Result:
    output: object = "unused"

    def __init__(self, *, input_tokens: object, output_tokens: object) -> None:
        self._usage = type(
            "Usage",
            (),
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
        )()

    def usage(self) -> object:
        return self._usage


class _Agent:
    def __init__(self, result: _Result) -> None:
        self._result = result

    def run_sync(self, prompt: str) -> _Result:
        return self._result


def _normalized_usage(*, input_tokens: object, output_tokens: object) -> dict[str, int]:
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
    assert _normalized_usage(input_tokens=input_tokens, output_tokens=output_tokens) == expected
