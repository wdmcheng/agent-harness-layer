"""真实调用 retry/deadline、动态预算与 side-effect unknown 合同。"""

from __future__ import annotations

import httpx
import pytest
from tests.contracts.controlled_real_model_runtime_composition_test_support import ResultDouble
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.adapters.models.pydantic_ai import (
    ControlledOpenAIClientFactory,
    ControlledOpenAITransport,
    ModelProviderError,
    PydanticAIModelProvider,
)
from agent_harness.config import HarnessSettings, ModelSettings, load_settings
from agent_harness.models import (
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.registry import (
    AgentModelPolicy,
)


class SequenceAgent:
    """按顺序返回异常或结果，精确暴露 adapter attempt call count。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def run(
        self, prompt: str, *, model_settings: object
    ) -> ResultDouble | ResultWithoutUsageDouble:
        del prompt, model_settings
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, (ResultDouble, ResultWithoutUsageDouble))
        return outcome


class ResultWithoutUsageDouble:
    """模拟 provider 已完成并返回文本，但没有报告任何 usage。"""

    output = "adapter-result-without-usage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body", "expected_completion"),
    [
        ([(b"X-Agent-Harness-Completion-State", b"not-started")], {"error": {}}, False),
        ([], {"error": {}}, None),
        (
            [
                (b"X-Agent-Harness-Completion-State", b"not-started"),
                (b"x-agent-harness-completion-state", b"not-started"),
            ],
            {"error": {}},
            None,
        ),
        (
            [(b"X-Agent-Harness-Completion-State", b"not-started,not-started")],
            {"error": {}},
            None,
        ),
        (
            [(b"X-Agent-Harness-Completion-State", b"not-started")],
            {"id": "partial-result", "error": {}},
            None,
        ),
    ],
)
async def test_transport_classifier_requires_exact_single_raw_header_and_no_response_evidence(
    headers: list[tuple[bytes, bytes]],
    body: dict[str, object],
    expected_completion: bool | None,
) -> None:
    """私有 transport 才能解释可信原始 header，重复/合并/body evidence 一律 unknown。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, headers=headers, json=body)

    transport = ControlledOpenAITransport(
        inner=httpx.MockTransport(handler),
        canonical_base_url="https://models.example.test/v1",
        api_key="fixture-secret",
        completion_classifier_ref="trusted_response_header_not_started",
        completion_classifier_version="v1",
    )
    request = httpx.Request(
        "POST",
        "https://models.example.test/v1/chat/completions",
        json={"model": "fixture-text-1"},
    )

    with pytest.raises(ModelProviderError) as exc_info:
        await transport.handle_async_request(request)

    assert exc_info.value.status_code == 429
    assert exc_info.value.completion_observed is expected_completion
    # 已收到 HTTP response 就证明请求已离开本进程；可信 false 只授权 retry，
    # 不能把该 attempt 改写成零副作用或允许退款。
    assert exc_info.value.side_effect_state == "started"
    await transport.aclose()


def _retry_settings(*, classifier: bool) -> HarnessSettings:
    """构造带可选可信 completion classifier 的完整 typed settings。"""

    overrides = real_model_override()
    deployment = overrides["model"]["deployments"]["real_primary"]  # type: ignore[index]
    policy = overrides["model"]["endpoint_policies"]["real_primary_endpoint"]  # type: ignore[index]
    # Linux 全量合同运行时需覆盖首次 Pydantic AI/SDK 冷启动；具体 read timeout
    # 仍保持 2000ms，避免把测试预算误当成 provider 超时语义。
    deployment["total_timeout_ms"] = 10_000  # type: ignore[index]
    deployment["max_attempts"] = 2  # type: ignore[index]
    deployment["max_retry_wait_ms"] = 100  # type: ignore[index]
    if classifier:
        deployment["retryable_http_statuses"] = [429, 503]  # type: ignore[index]
        deployment["completion_classifier_ref"] = "trusted_response_header_not_started"  # type: ignore[index]
        deployment["completion_classifier_version"] = "v1"  # type: ignore[index]
        policy["completion_classifiers"] = [  # type: ignore[index]
            {"ref": "trusted_response_header_not_started", "version": "v1"}
        ]
    return load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def _retry_route(*, classifier: bool) -> tuple[ModelSettings, ModelRequest, AgentModelPolicy]:
    settings = _retry_settings(classifier=classifier)
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        prompt="retry fixture",
        max_output_tokens=9,
    )
    agent_policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    return settings.model, request, agent_policy


@pytest.mark.asyncio
async def test_retry_requires_trusted_versioned_completion_signal() -> None:
    """只有绑定 policy/version 的可信 false completion 信号允许第二次 attempt。"""

    model_settings, request, policy = _retry_route(classifier=True)
    agent = SequenceAgent(
        [
            ModelProviderError(
                "model.provider_failed",
                status_code=429,
                retry_after_ms=0,
                completion_observed=False,
                side_effect_state="started",
            ),
            ResultDouble(),
        ]
    )
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: agent,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    response = await router.execute(request, plan=plan)

    assert agent.calls == 2
    assert [item.outcome for item in response.attempts] == [
        "retryable_status",
        "completed",
    ]
    assert response.attempts[0].side_effect_state == "started"
    assert plan.reserved_token_bound == plan.per_attempt_token_bound * 2


@pytest.mark.asyncio
async def test_locked_sdk_chain_preserves_trusted_429_for_controlled_retry() -> None:
    """真实 OpenAI/Pydantic AI 组合链不得把可信 429 分类降级成连接异常。"""

    model_settings, request, policy = _retry_route(classifier=True)
    calls = 0

    async def handler(inbound: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                request=inbound,
                headers={
                    "Retry-After": "0",
                    "X-Agent-Harness-Completion-State": "not-started",
                },
                json={"error": {"type": "rate_limit"}},
            )
        return httpx.Response(
            200,
            request=inbound,
            json={
                "id": "chatcmpl-retry-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-text-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "retry-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        client_factory=factory,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    try:
        response = await router.execute(request, plan=plan)
    finally:
        await factory.aclose()

    assert calls == 2
    assert response.output_text == "retry-ok"
    assert [attempt.outcome for attempt in response.attempts] == [
        "retryable_status",
        "completed",
    ]


@pytest.mark.asyncio
async def test_locked_sdk_read_timeout_is_unknown_and_never_retried() -> None:
    """锁定 SDK 的 read timeout 已越过 connect 边界，只能按未知副作用封闭。"""

    model_settings, request, policy = _retry_route(classifier=True)
    calls = 0

    async def handler(inbound: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture read timeout", request=inbound)

    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        client_factory=factory,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    try:
        with pytest.raises(ModelProviderError) as exc_info:
            await router.execute(request, plan=plan)
    finally:
        await factory.aclose()

    assert calls == 1
    assert exc_info.value.code == "model.provider_side_effect_unknown"
    assert exc_info.value.side_effect_state == "unknown"
    assert len(exc_info.value.attempts) == 1
    assert exc_info.value.attempts[0].side_effect_state == "unknown"
    assert exc_info.value.attempts[0].outcome == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_locked_sdk_connect_before_send_is_not_started_and_retryable(
    connect_error: type[httpx.RequestError],
) -> None:
    """真实 SDK 的连接前失败没有远端副作用，可在冻结上限内安全重试。"""

    model_settings, request, policy = _retry_route(classifier=True)
    calls = 0

    async def handler(inbound: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise connect_error("fixture connect-before-send", request=inbound)
        return httpx.Response(
            200,
            request=inbound,
            json={
                "id": "chatcmpl-connect-retry",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-text-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "connect-retry-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        client_factory=factory,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    try:
        response = await router.execute(request, plan=plan)
    finally:
        await factory.aclose()

    assert calls == 2
    assert response.output_text == "connect-retry-ok"
    assert response.attempts[0].side_effect_state == "not_started"
    assert response.attempts[0].completion_observed is False
    assert response.attempts[0].outcome == "retryable_status"
    assert response.attempts[0].budget_charge_tokens == 0
    assert response.attempts[1].outcome == "completed"


@pytest.mark.asyncio
async def test_untrusted_or_unknown_completion_state_never_retries() -> None:
    """缺 classifier 或 completion unknown 时 429 也不得自动重试。"""

    model_settings, request, policy = _retry_route(classifier=False)
    agent = SequenceAgent(
        [
            ModelProviderError(
                "model.provider_side_effect_unknown",
                status_code=429,
                completion_observed=None,
                side_effect_state="unknown",
            ),
            ResultDouble(),
        ]
    )
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: agent,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)

    with pytest.raises(ModelProviderError) as exc_info:
        await router.execute(request, plan=plan)
    assert exc_info.value.code == "model.provider_side_effect_unknown"
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_retry_attempts_keep_started_unresolved_usage_after_trusted_retry() -> None:
    """可信 false 允许 retry，但首个 response attempt 仍为 started 且保持未决。"""

    model_settings, request, policy = _retry_route(classifier=True)
    agent = SequenceAgent(
        [
            ModelProviderError(
                "model.provider_failed",
                status_code=429,
                retry_after_ms=0,
                completion_observed=False,
                side_effect_state="started",
            ),
            ResultDouble(),
        ]
    )
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible", agent_factory=lambda _plan: agent
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible", default_model="fixture-text-1"
        ),
        providers={"openai-compatible": provider},
        model_settings=model_settings,
    )
    response = await router.execute(request, plan=router.plan(request, agent_policy=policy))

    assert response.attempts[0].side_effect_state == "started"
    assert response.attempts[0].input_tokens is None
    assert response.attempts[1].input_tokens == 3
    assert response.attempts[1].output_tokens == 2
    # 首个 started attempt 缺 usage 后，响应级聚合也不能只冒充最后一次成功 usage。
    assert response.token_usage == {}
    assert response.cost_usd is None
    assert response.cost_status == "unavailable"
