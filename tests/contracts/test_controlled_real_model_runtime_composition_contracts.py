"""真实 adapter、lazy client 与默认 fake 离线组合合同。"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from tests.contracts.controlled_real_model_runtime_composition_test_support import (
    AsyncAgentDouble,
    FrozenSnapshotExecutor,
    controlled_route,
)
from tests.contracts.provider_neutral_structured_output_test_support import (
    fixture_output_schema_identity,
)
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    tool_intent_catalog_fixture,
    tool_intent_override,
    tool_intent_router_and_policy_fixture,
)

from agent_harness import cli as harness_cli
from agent_harness.adapters.models import _pydantic_ai_client as pydantic_ai_client
from agent_harness.adapters.models.pydantic_ai import (
    ControlledOpenAIClientFactory,
    PydanticAIModelProvider,
)
from agent_harness.audit import AuditService
from agent_harness.config import load_settings
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelInvocationService,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import (
    RunOrchestrator,
    RunStatus,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage, run_migrations


@pytest.mark.asyncio
async def test_composition_registers_async_real_provider_double_and_keeps_fake_offline() -> None:
    """真实 adapter 使用 async Agent.run，fake/local 是否联网不受真实配置存在影响。"""

    settings, request, policy, model_settings = controlled_route()
    agent = AsyncAgentDouble()
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

    assert response.output_text == "adapter-result"
    assert response.token_usage == {"input_tokens": 3, "output_tokens": 2}
    assert agent.calls == [("adapter hello", 17)]
    assert settings.model.deployments["fake_default"].provider_kind == "fake"


@pytest.mark.asyncio
async def test_openai_sdk_ambient_env_cannot_change_controlled_client_or_outbound_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK ambient identity/header/base-url/proxy 不得改变 typed plan 的真实出站请求。"""

    for key, value in {
        "OPENAI_API_KEY": "ambient-key",
        "OPENAI_ADMIN_KEY": "ambient-admin",
        "OPENAI_ORG_ID": "ambient-org",
        "OPENAI_PROJECT_ID": "ambient-project",
        "OPENAI_WEBHOOK_SECRET": "ambient-webhook",
        "OPENAI_BASE_URL": "https://evil.example.test/v1",
        # openai 2.44.0 按换行的 ``Header: value`` 解析该变量；JSON 不会走真实分支。
        "OPENAI_CUSTOM_HEADERS": (
            "Authorization: Bearer ambient\nX-Evil: 1\nIdempotency-Key: ambient-fixed"
        ),
        "HTTPS_PROXY": "https://proxy.example.test",
    }.items():
        monkeypatch.setenv(key, value)

    _settings, request, policy, model_settings = controlled_route()
    captured: list[httpx.Request] = []

    async def handler(inbound: httpx.Request) -> httpx.Response:
        captured.append(inbound)
        return httpx.Response(
            200,
            request=inbound,
            json={
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-text-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    # 形成 route plan 不得构造 SDK client。
    provider_stub = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: AsyncAgentDouble(),
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider_stub},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    assert factory.client_construction_count == 0

    lease = await factory.acquire(plan)
    completion = await lease.openai_client.chat.completions.create(
        model=plan.model,
        messages=[{"role": "user", "content": "safe fixture"}],
        max_tokens=plan.output_token_cap,
    )

    assert completion.choices[0].message.content == "ok"
    assert factory.client_construction_count == 1
    assert len(captured) == 1
    outbound = captured[0]
    assert str(outbound.url).startswith("https://models.example.test/v1/")
    assert outbound.headers["authorization"] == "Bearer phase18-secret-fixture"
    assert "x-evil" not in outbound.headers
    assert "idempotency-key" not in outbound.headers
    assert "openai-organization" not in outbound.headers
    assert "openai-project" not in outbound.headers
    assert "ambient" not in json.dumps(dict(outbound.headers)).lower()
    assert os.environ["OPENAI_API_KEY"] == "ambient-key"
    await factory.aclose()
    await factory.aclose()


@pytest.mark.asyncio
async def test_controlled_client_has_immutable_untrusted_context_instruction_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有tool-intent使用可预算的SDK instruction角色固定不可信上下文边界。"""

    _settings, request, policy, model_settings = controlled_route()
    captured: list[dict[str, object]] = []

    def recording_agent(model: object, **kwargs: object) -> AsyncAgentDouble:
        captured.append({"model": model, **kwargs})
        return AsyncAgentDouble()

    monkeypatch.setattr(pydantic_ai_client, "Agent", recording_agent)
    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: httpx.MockTransport(
            lambda inbound: httpx.Response(200, request=inbound, json={})
        ),
    )
    provider_stub = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: AsyncAgentDouble(),
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider_stub},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    await factory.acquire(plan)
    await factory.aclose()

    assert captured[0]["instructions"] is None

    tool_settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=tool_intent_override(),
    )
    tool_router, tool_policy = tool_intent_router_and_policy_fixture()
    tool_request = request.model_copy(
        update={
            "prompt": "tool turn",
            "capability": "tool_intent",
            "max_output_tokens": 8,
        }
    )
    tool_plan = tool_router.plan_tool_intent(
        tool_request,
        tool_catalog=tool_intent_catalog_fixture(),
        agent_policy=tool_policy,
    )
    tool_factory = ControlledOpenAIClientFactory(
        model_settings=tool_settings.model,
        transport_factory=lambda: httpx.MockTransport(
            lambda inbound: httpx.Response(200, request=inbound, json={})
        ),
    )
    await tool_factory.acquire(tool_plan)
    await tool_factory.aclose()

    instructions = captured[1]["instructions"]
    assert isinstance(instructions, str)
    assert instructions == "RULES>UNTRUSTED"
    assert len(instructions.encode("utf-8")) <= tool_plan.input_envelope_token_bound
    assert request.prompt not in instructions
    assert captured[1]["tools"] == ()


@pytest.mark.asyncio
async def test_model_invocation_close_cascades_router_provider_and_client_factory() -> None:
    """组合根只关闭 provider-neutral invocation，资源链必须幂等下沉到 client。"""

    _settings, request, policy, model_settings = controlled_route()
    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: httpx.MockTransport(
            lambda inbound: httpx.Response(200, request=inbound, json={})
        ),
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
    invocation = ModelInvocationService(
        router=router,
        storage=cast(Any, object()),
        event_bus=cast(Any, object()),
    )
    plan = router.plan(request, agent_policy=policy)
    await factory.acquire(plan)

    await invocation.aclose()
    await invocation.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await factory.acquire(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["async_openai", "provider", "model", "agent"])
async def test_client_factory_closes_partial_resources_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """SDK 组合中途失败也必须关闭已取得所有权的资源，且不得留下可复用 lease。"""

    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.request_calls = 0
            self.close_calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.request_calls += 1
            raise AssertionError(f"client construction must not send HTTP: {request.url}")

        async def aclose(self) -> None:
            self.close_calls += 1

    transport = CountingTransport()
    _settings, request, policy, model_settings = controlled_route()
    provider_stub = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: AsyncAgentDouble(),
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider_stub},
        model_settings=model_settings,
    )
    plan = router.plan(request, agent_policy=policy)
    factory = ControlledOpenAIClientFactory(
        model_settings=model_settings,
        transport_factory=lambda: transport,
    )

    def fail_construction(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(f"fixture {failure_stage} construction failure")

    constructor_symbol = {
        "async_openai": "AsyncOpenAI",
        "provider": "OpenAIProvider",
        "model": "OpenAIChatModel",
        "agent": "Agent",
    }[failure_stage]
    monkeypatch.setattr(pydantic_ai_client, constructor_symbol, fail_construction)

    with pytest.raises(RuntimeError, match=f"fixture {failure_stage}") as exc_info:
        await factory.acquire(plan)

    assert transport.request_calls == 0
    assert transport.close_calls == 1
    assert factory.client_construction_count == 0
    assert "phase18-secret-fixture" not in str(exc_info.value)

    await factory.aclose()
    await factory.aclose()
    assert transport.close_calls == 1


def test_cli_run_uses_one_event_loop_for_execution_and_provider_cleanup() -> None:
    """CLI 成功、失败和取消都必须在创建 client 的同一顶层协程完成清理。"""

    source = inspect.getsource(harness_cli.run)

    assert source.count("asyncio.run(") == 1
    assert (
        "finally:\n                await close_agent_execution_services(executor_services)"
        in source
    )
    assert "await storage.dispose()" in source


@pytest.mark.asyncio
async def test_full_invocation_uses_v2_snapshot_policy_budget_audit_and_old_path(
    tmp_path: Path,
) -> None:
    """真实 provider double 也必须经 v2 快照、policy audit、预算预约和耐久 evidence。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    database = tmp_path / "controlled-runtime.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    event_bus = EventBus(
        sink=event_sink,
        capacity_storage=storage,
    )
    audit = AuditService(storage)
    policy_engine = PolicyEngine(provider=YamlPolicyProvider(), audit=audit)
    agent_policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    registry = AgentRegistry(
        [
            AgentDescriptor(
                agent_id="agent-real",
                version="v1",
                name="真实模型合同",
                description="只使用离线 provider double",
                input_schema_ref="fixture.Input",
                output_schema_ref="fixture.Output",
                output_schema_identity=fixture_output_schema_identity(),
                config_ref="fixture/config.yaml",
                tool_policy=AgentToolPolicy(allowed_tools=[]),
                model_policy=agent_policy,
                budget=AgentBudget(max_tokens_per_run=4096, max_cost_usd_per_run=1.0),
                eval_dataset=None,
                delegation_targets=[],
            )
        ]
    )
    seen_plans: list[ModelRoutePlan] = []
    agent = AsyncAgentDouble()

    def agent_factory(plan: ModelRoutePlan) -> AsyncAgentDouble:
        seen_plans.append(plan)
        return agent

    provider = PydanticAIModelProvider(agent_factory=agent_factory)
    shared_budget = SharedBudgetRuntime(settings=settings, registry=registry)
    invocation = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_provider="openai-compatible",
                default_model="fixture-text-1",
            ),
            providers={"openai-compatible": provider},
            model_settings=settings.model,
        ),
        storage=storage,
        event_bus=event_bus,
        shared_budget=shared_budget,
        agent_policy_resolver=lambda agent_id: registry.get(agent_id).model_policy,
        policy_engine=policy_engine,
    )
    executor = FrozenSnapshotExecutor(settings)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=settings.identity.default,
        executor_resolver=lambda _agent_id: executor,
        executor_services={
            "model_invocation": invocation,
            "shared_budget": shared_budget,
        },
    )
    try:
        result = await orchestrator.start_run(agent_id="agent-real", input={"prompt": "x"})
        assert result.status is RunStatus.COMPLETED
        assert len(seen_plans) == 1
        assert seen_plans[0].canonical_base_url == "https://models.example.test/v1"
        assert agent.calls == [("snapshot route", 8)]
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger(
                settings.identity.default.tenant_id, result.run_id
            )
            audits = await uow.audit_logs.list_for_tenant(settings.identity.default.tenant_id)
        assert ledger is not None and ledger.snapshot_id.startswith("budget-tree-v2:")
        assert any(item.action == "policy.decision" for item in audits)
        final_events = [
            event
            for event in await event_sink.read(run_id=result.run_id)
            if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED
        ]
        assert len(final_events) == 1
        assert final_events[0].payload is not None
        usage = cast(dict[str, object], final_events[0].payload["usage"])
        decision = cast(dict[str, object], usage["decision"])
        route = cast(dict[str, object], decision["route"])
        assert route["trusted_input_token_bound"] == seen_plans[0].trusted_input_token_bound
        assert route["output_token_cap"] == seen_plans[0].output_token_cap
        assert route["per_attempt_token_bound"] == seen_plans[0].per_attempt_token_bound
        assert route["reserved_token_bound"] == seen_plans[0].reserved_token_bound
        attempts = cast(list[dict[str, object]], decision["attempts"])
        assert attempts == [
            {
                "attempt": 1,
                "outcome": "completed",
                "side_effect_state": "started",
                "completion_observed": True,
                "http_status": None,
                "retry_after_ms": None,
                "input_tokens": 3,
                "output_tokens": 2,
                "cost_usd": 0.000007,
                "cost_status": "estimated",
                "budget_charge_tokens": 5,
                "budget_charge_cost_usd": 0.000007,
                "latency_ms": attempts[0]["latency_ms"],
                "error_code": None,
            }
        ]
        assert decision["budget_charge"] == {
            "charged_tokens": 5,
            "charged_cost_usd": 0.000007,
            "charge_status": "actual",
            "unresolved_attempts": [],
        }
    finally:
        await storage.dispose()
