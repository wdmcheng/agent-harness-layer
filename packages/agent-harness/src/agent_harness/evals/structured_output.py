"""Provider-neutral structured output 的确定性本地评分。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelResponse
from agent_harness.models.structured import canonical_structured_json


class StructuredOutputEvalScore(HarnessDTO):
    """结构化输出有效性指标；只有耐久valid终态可以得到一分。"""

    schema_version: Literal["structured-output-eval-score-v1"] = "structured-output-eval-score-v1"
    metric: Literal["structured_output_valid"] = "structured_output_valid"
    value: float = Field(ge=0, le=1)
    passed: bool
    terminal_status: Literal[
        "valid",
        "invalid",
        "extra_fields",
        "repair_exhausted",
        "failed",
        "needs_review",
    ]
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_score_union(self) -> StructuredOutputEvalScore:
        """分值、passed 与终态必须逐值一致，避免 unknown 被统计为成功。"""

        expected = self.terminal_status == "valid"
        if self.passed != expected or self.value != (1.0 if expected else 0.0):
            raise ValueError("structured eval score does not match terminal status")
        if expected != (self.error_code is None):
            raise ValueError("structured eval error code does not match terminal status")
        return self


def score_structured_output(
    *,
    response: ModelResponse | None,
    terminal_status: Literal[
        "valid",
        "invalid",
        "extra_fields",
        "repair_exhausted",
        "failed",
        "needs_review",
    ],
    error_code: str | None,
) -> StructuredOutputEvalScore:
    """只消费核心终态与 provider-neutral response，不调用模型或读取 raw candidate。"""

    if terminal_status == "valid":
        if response is None or response.structured_output is None or error_code is not None:
            raise ValueError("valid structured eval requires a provider-neutral structured result")
        if response.output_text != canonical_structured_json(response.structured_output.value):
            raise ValueError("valid structured eval requires canonical output_text")
        return StructuredOutputEvalScore(
            value=1.0,
            passed=True,
            terminal_status="valid",
        )
    if response is not None or not error_code:
        raise ValueError("non-valid structured eval requires only a stable error code")
    return StructuredOutputEvalScore(
        value=0.0,
        passed=False,
        terminal_status=terminal_status,
        error_code=error_code,
    )


__all__ = ["StructuredOutputEvalScore", "score_structured_output"]
