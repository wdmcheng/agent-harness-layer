"""Fake model adapter 的显式 tool-intent 脚本与零工具执行合同。"""

from __future__ import annotations

import pytest

from agent_harness.models import (
    FakeModelProvider,
    FakeToolIntentScript,
    ModelAttemptEvidence,
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ProviderToolIntentCandidate,
)
from agent_harness.models.tool_catalog import ToolIntentRequestIdentity


def _attempt() -> ModelAttemptEvidence:
    """每个脚本结果只携带一个已完成的本地 provider attempt。"""

    return ModelAttemptEvidence(
        attempt=1,
        side_effect_state="started",
        outcome="completed",
        completion_observed=True,
        input_tokens=3,
        output_tokens=2,
        cost_status="unavailable",
        latency_ms=0,
    )


def _plan(*, catalog_json: str) -> ModelRoutePlan:
    """构造只测试 adapter 投影所需的冻结 tool route。"""

    identity = ToolIntentRequestIdentity(
        model_catalog_digest="a" * 64,
        tool_catalog_digest="b" * 64,
        tool_catalog_utf8_bytes=len(catalog_json.encode("utf-8")),
        max_tool_catalog_utf8_bytes=512,
        trusted_input_token_bound=8,
        output_token_cap=8,
    )
    return ModelRoutePlan(
        provider="fake",
        provider_kind="fake",
        model="fake-basic",
        capability="tool_intent",
        decision=ModelDecision(action="call", estimated_tokens=8),
        tool_request_identity=identity,
        tool_request_identity_digest=identity.digest,
        provider_tool_catalog_json=catalog_json,
        request_shape_ref="single-user-text-with-tool-catalog",
        request_shape_version="v1",
        output_token_cap=8,
        per_attempt_token_bound=8,
        reserved_token_bound=8,
        trusted_token_bound=8,
        trusted_cost_bound=None,
    )


@pytest.mark.asyncio
async def test_fake_tool_intent_script_exposes_final_and_tool_without_callback() -> None:
    """离线 adapter 只顺序返回显式结果，不注册 handler 或执行 provider-native tool。"""

    final = ModelResponse(
        provider="fake",
        model="fake-basic",
        output_text="done",
        decision=ModelDecision(action="call", estimated_tokens=8),
        token_usage={"input_tokens": 3, "output_tokens": 2},
        attempts=[_attempt()],
    )
    intent = ProviderToolIntentCandidate(
        provider="fake",
        model="fake-basic",
        tool_name="search",
        arguments={"q": "weather"},
        tool_schema_ref="search-input",
        tool_schema_version="v1",
        tool_schema_digest="d" * 64,
        attempts=[_attempt()],
    )
    provider = FakeModelProvider(tool_intent_script=FakeToolIntentScript(results=(final, intent)))
    catalog_json = '{"schema_version":"provider-tool-catalog-v1","tools":[]}'
    request = ModelRequest(
        provider="fake",
        model="fake-basic",
        prompt="weather",
        capability="tool_intent",
        max_output_tokens=8,
    )

    first = await provider.prepare_tool_intent(
        request,
        plan=_plan(catalog_json=catalog_json),
        tool_catalog_json=catalog_json.encode("utf-8"),
    )
    assert await first.send_tool_intent() == final
    await first.aclose()
    second = await provider.prepare_tool_intent(
        request,
        plan=_plan(catalog_json=catalog_json),
        tool_catalog_json=catalog_json.encode("utf-8"),
    )
    assert await second.send_tool_intent() == intent
    await second.aclose()

    assert provider.tool_intent_prepare_count == 2
    assert provider.tool_intent_send_count == 2
    assert provider.tool_intent_close_count == 2
    assert provider.provider_native_tool_execution_count == 0


@pytest.mark.asyncio
async def test_fake_tool_intent_prepare_rejects_catalog_drift_without_consuming_script() -> None:
    """Adapter 只能接收 route 冻结 bytes，漂移不得推进脚本或制造发送副作用。"""

    final = ModelResponse(
        provider="fake",
        model="fake-basic",
        output_text="done",
        decision=ModelDecision(action="call", estimated_tokens=8),
        token_usage={"input_tokens": 3, "output_tokens": 2},
        attempts=[_attempt()],
    )
    provider = FakeModelProvider(tool_intent_script=FakeToolIntentScript(results=(final,)))
    frozen = '{"schema_version":"provider-tool-catalog-v1","tools":[]}'
    request = ModelRequest(
        provider="fake",
        model="fake-basic",
        prompt="weather",
        capability="tool_intent",
        max_output_tokens=8,
    )

    with pytest.raises(ValueError):
        await provider.prepare_tool_intent(
            request,
            plan=_plan(catalog_json=frozen),
            tool_catalog_json=b'{"schema_version":"provider-tool-catalog-v1","tools":[{}]}',
        )

    assert provider.tool_intent_prepare_count == 0
    assert provider.tool_intent_send_count == 0
    assert provider.provider_native_tool_execution_count == 0
