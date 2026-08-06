"""Adapter candidate 到核心 ToolIntent 的 provider-neutral 合同。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent_harness.models import ModelAttemptEvidence
from agent_harness.models.tool_intent import (
    ProviderToolIntentCandidate,
    ToolIntentValidationError,
    normalize_provider_tool_intent,
)

_LOOP_ID = "1" * 64
_USAGE_CALL_ID = "2" * 64
_CATALOG_DIGEST = "3" * 64
_SCHEMA_DIGEST = "4" * 64


def _attempt() -> ModelAttemptEvidence:
    """Candidate 只允许一个已完成的 provider-local attempt。"""

    return ModelAttemptEvidence(
        attempt=1,
        side_effect_state="started",
        outcome="completed",
        completion_observed=True,
        input_tokens=3,
        output_tokens=2,
        cost_status="unavailable",
        latency_ms=4,
    )


def _candidate(*, provider: str = "provider-a") -> ProviderToolIntentCandidate:
    """构造不携带核心身份或 vendor 对象的 exact candidate。"""

    return ProviderToolIntentCandidate(
        provider=provider,
        model="model-a",
        tool_name="search",
        arguments={"q": "agent harness"},
        tool_schema_ref="search-input",
        tool_schema_version="v1",
        tool_schema_digest=_SCHEMA_DIGEST,
        attempts=[_attempt()],
    )


def _normalize(
    candidate: object,
    *,
    provider: str = "provider-a",
    turn_ordinal: int = 1,
):
    """注入只能来自 bound runtime 的身份与冻结 catalog 事实。"""

    return normalize_provider_tool_intent(
        candidate,
        expected_provider=provider,
        expected_model="model-a",
        expected_tool_name="search",
        expected_tool_schema_ref="search-input",
        expected_tool_schema_version="v1",
        expected_tool_schema_digest=_SCHEMA_DIGEST,
        loop_id=_LOOP_ID,
        turn_ordinal=turn_ordinal,
        model_usage_call_id=_USAGE_CALL_ID,
        catalog_digest=_CATALOG_DIGEST,
    )


def test_candidate_exact_shape_contains_one_usage_attempt() -> None:
    """Candidate 仅含 provider-neutral proposal 与单次 usage/attempt。"""

    payload = _candidate().model_dump(mode="json")

    assert set(payload) == {
        "schema_version",
        "provider",
        "model",
        "tool_name",
        "arguments",
        "tool_schema_ref",
        "tool_schema_version",
        "tool_schema_digest",
        "attempts",
    }
    assert payload["schema_version"] == "provider-tool-intent-candidate-v1"
    assert len(payload["attempts"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("loop_id", _LOOP_ID),
        ("turn_ordinal", 1),
        ("tool_call_id", "5" * 64),
        ("model_usage_call_id", _USAGE_CALL_ID),
        ("raw_response", {"opaque": True}),
        ("client", object()),
        ("handler", lambda: None),
    ],
)
def test_candidate_rejects_core_identity_and_vendor_fields(field: str, value: object) -> None:
    """Provider、SDK 和调用方均不能把受信身份或可执行对象塞进 candidate。"""

    payload = _candidate().model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        ProviderToolIntentCandidate.model_validate(payload)


@pytest.mark.parametrize(
    "attempts",
    [
        [],
        [_attempt(), _attempt()],
        [
            ModelAttemptEvidence(
                attempt=1,
                side_effect_state="unknown",
                outcome="unknown",
                completion_observed=None,
                error_code="model.provider_side_effect_unknown",
                latency_ms=1,
            )
        ],
    ],
)
def test_candidate_rejects_non_completed_single_attempt(
    attempts: list[ModelAttemptEvidence],
) -> None:
    """未知、失败或多次请求不能伪装成一个合法工具提议。"""

    payload = _candidate().model_dump(mode="python")
    payload["attempts"] = attempts
    with pytest.raises(ValidationError):
        ProviderToolIntentCandidate.model_validate(payload)


def test_candidate_rejects_non_json_arguments_without_stringifying() -> None:
    """Adapter 原始 SDK 对象不能被字符串化后越过 candidate 边界。"""

    payload = _candidate().model_dump(mode="python")
    payload["arguments"] = {"q": object()}
    with pytest.raises(ValidationError):
        ProviderToolIntentCandidate.model_validate(payload)


def test_core_derives_same_identity_across_provider_doubles() -> None:
    """Provider id 不参与受信 loop/turn/catalog/arguments 身份。"""

    first = _normalize(_candidate(provider="provider-a"), provider="provider-a")
    second = _normalize(_candidate(provider="provider-b"), provider="provider-b")

    assert first == second
    assert first.tool_call_id
    assert first.arguments_digest
    assert first.model_usage_call_id == _USAGE_CALL_ID


def test_core_identity_changes_with_turn_or_canonical_arguments() -> None:
    """同一输入可重算，turn 或 canonical arguments 漂移必须改变 call identity。"""

    first = _normalize(_candidate())
    repeated = _normalize(_candidate())
    next_turn = _normalize(_candidate(), turn_ordinal=2)
    changed_candidate = _candidate().model_copy(
        update={"arguments": {"q": "different"}},
        deep=True,
    )
    changed_arguments = _normalize(changed_candidate)

    assert first == repeated
    assert first.tool_call_id != next_turn.tool_call_id
    assert first.tool_call_id != changed_arguments.tool_call_id
    assert first.arguments_digest != changed_arguments.arguments_digest


@pytest.mark.parametrize(
    "mutation",
    [
        {"provider": "provider-b"},
        {"model": "model-b"},
        {"tool_name": "write_file"},
        {"tool_schema_ref": "other-input"},
        {"tool_schema_version": "v2"},
        {"tool_schema_digest": "6" * 64},
    ],
)
def test_core_rejects_candidate_binding_drift(mutation: dict[str, object]) -> None:
    """Provider/model/tool/schema 任一漂移都不能生成可执行 ToolIntent。"""

    payload = _candidate().model_dump(mode="python")
    payload.update(deepcopy(mutation))
    candidate = ProviderToolIntentCandidate.model_validate(payload)

    with pytest.raises(ToolIntentValidationError) as failure:
        _normalize(candidate)

    assert failure.value.code == "model.tool_intent_invalid"
