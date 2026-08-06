"""模型单轮判别联合的公开 red→green 合同。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_harness.models import (
    ModelDecision,
    ModelResponse,
    ModelTurnResult,
    OutputSchemaIdentity,
    StructuredOutputResult,
    ToolIntent,
    structured_digest,
)

_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64


def _text_response() -> ModelResponse:
    """构造不含 structured payload 的既有文本响应。"""

    return ModelResponse(
        provider="provider-a",
        model="model-a",
        output_text="done",
        decision=ModelDecision(action="complete", estimated_tokens=1),
        token_usage={"input_tokens": 1, "output_tokens": 1},
    )


def _structured_response() -> ModelResponse:
    """构造复用 MOD-005 成功结果的既有模型响应。"""

    schema_identity = OutputSchemaIdentity(
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
        digest=_DIGEST,
    )
    result = StructuredOutputResult(
        schema_identity=schema_identity,
        value={"answer": "done"},
        repair_count=0,
        provider_request_count=1,
        replay_identity=_OTHER_DIGEST,
    )
    return ModelResponse(
        provider="provider-a",
        model="model-a",
        output_text='{"answer":"done"}',
        decision=ModelDecision(action="complete", estimated_tokens=1),
        token_usage={"input_tokens": 1, "output_tokens": 1},
        structured_output=result,
    )


def _tool_intent() -> ToolIntent:
    """构造只含核心受信身份的 provider-neutral 工具意图。"""

    return ToolIntent(
        loop_id=_DIGEST,
        turn_ordinal=1,
        tool_call_id=_OTHER_DIGEST,
        tool_name="search",
        arguments={"q": "agent harness"},
        arguments_digest=structured_digest({"q": "agent harness"}),
        tool_schema_ref="search-input",
        tool_schema_version="v1",
        tool_schema_digest=_OTHER_DIGEST,
        model_usage_call_id=_DIGEST,
        catalog_digest=_OTHER_DIGEST,
    )


def _adapter() -> TypeAdapter[ModelTurnResult]:
    """通过公开类型执行与真实调用方相同的判别解析。"""

    return TypeAdapter(ModelTurnResult)


def test_model_turn_result_accepts_each_exact_public_branch() -> None:
    """三个合法分支只保留本分支 payload，并逐值复用既有结果 DTO。"""

    text = _adapter().validate_python({"kind": "final_text", "response": _text_response()})
    structured = _adapter().validate_python(
        {"kind": "final_structured", "response": _structured_response()}
    )
    tool = _adapter().validate_python({"kind": "tool_intent", "intent": _tool_intent()})

    assert text.kind == "final_text"
    assert text.response == _text_response()
    assert structured.kind == "final_structured"
    assert structured.response == _structured_response()
    assert tool.kind == "tool_intent"
    assert tool.intent == _tool_intent()


@pytest.mark.parametrize("kind", ["", "text", "structured", "tool_call", "unknown"])
def test_model_turn_result_rejects_unknown_discriminator(kind: str) -> None:
    """任意 JSON 或 SDK 外观都不能替代冻结的 kind 判别。"""

    with pytest.raises(ValidationError):
        _adapter().validate_python({"kind": kind, "response": _text_response()})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "final_text",
            "response": _text_response(),
            "intent": _tool_intent(),
        },
        {
            "kind": "tool_intent",
            "intent": _tool_intent(),
            "response": _text_response(),
        },
    ],
)
def test_model_turn_result_rejects_mixed_payloads(payload: dict[str, object]) -> None:
    """混合最终结果与工具意图必须在 Registry 或工具副作用前关闭失败。"""

    with pytest.raises(ValidationError):
        _adapter().validate_python(payload)


@pytest.mark.parametrize("kind", ["final_text", "final_structured", "tool_intent"])
def test_model_turn_result_rejects_missing_branch_payload(kind: str) -> None:
    """判别值存在但必要 payload 缺失时不得构造半成品结果。"""

    with pytest.raises(ValidationError):
        _adapter().validate_python({"kind": kind})


def test_model_turn_result_rejects_cross_branch_response_semantics() -> None:
    """文本与 structured 分支不得借同一 ModelResponse 跨 capability 冒充。"""

    with pytest.raises(ValidationError):
        _adapter().validate_python({"kind": "final_text", "response": _structured_response()})
    with pytest.raises(ValidationError):
        _adapter().validate_python({"kind": "final_structured", "response": _text_response()})


def test_model_turn_result_rejects_adapter_raw_fields() -> None:
    """raw response、SDK event 与 client 不能混入公共结果。"""

    with pytest.raises(ValidationError):
        _adapter().validate_python(
            {
                "kind": "tool_intent",
                "intent": _tool_intent(),
                "raw_response": {"sdk_tool_call": "opaque"},
            }
        )
