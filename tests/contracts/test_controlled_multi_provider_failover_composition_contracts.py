"""多候选 client/catalog/Bulkhead 生命周期隔离与默认离线合同。"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    bound_failover_invocation,
    chain_settings,
    downstream_chain_policy,
)
from tests.contracts.controlled_real_model_runtime_composition_test_support import (
    AsyncAgentDouble,
)

from agent_harness.adapters.models._pydantic_ai_client import ModelProviderError
from agent_harness.adapters.models._pydantic_ai_streaming import (
    AgentRunResultEvent,
    PartStartEvent,
    TextPart,
)
from agent_harness.adapters.models.pydantic_ai import (
    ControlledOpenAIClientFactory,
    PydanticAIModelProvider,
)
from agent_harness.models import (
    FakeModelProvider,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
)


def test_shared_budget_controlled_route_requires_typed_agent_descriptor() -> None:
    """已解析的 Agent descriptor 必须保持窄类型，不能用 Any 绕过静态校验。"""

    source_path = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("packages/agent-harness/src/agent_harness/runtime/_shared_budget_snapshot.py")
    )
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    builder = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "SharedBudgetSnapshotBuilder"
    )
    method = next(
        node
        for node in builder.body
        if isinstance(node, ast.FunctionDef) and node.name == "_controlled_model_route"
    )
    descriptor = next(
        argument for argument in method.args.kwonlyargs if argument.arg == "descriptor"
    )

    assert descriptor.annotation is not None
    assert ast.unparse(descriptor.annotation) == "AgentDescriptor"


@dataclass
class _AdapterUsage:
    """模拟真实 adapter 最终结果的最小 usage 读面。"""

    input_tokens: int = 3
    output_tokens: int = 2


class _AdapterResult:
    """同时供 completion 与 stream 公共 façade 使用的合法 SDK 结果。"""

    output = "adapter-secondary"

    def usage(self) -> _AdapterUsage:
        return _AdapterUsage()


class _ClientNotStartedAdapterAgent:
    """复现受控 transport 在 connect/client 边界给 adapter 的封闭事实。"""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def run(self, _prompt: str, *, model_settings: object) -> object:
        del model_settings
        self._calls.append("real_primary")
        raise ModelProviderError(
            "model.provider_failed",
            completion_observed=False,
            side_effect_state="not_started",
        )

    @asynccontextmanager
    async def run_stream_events(self, _prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        del model_settings

        async def events():  # type: ignore[no-untyped-def]
            self._calls.append("real_primary")
            raise ModelProviderError(
                "model.provider_failed",
                completion_observed=False,
                side_effect_state="not_started",
            )
            yield

        yield events()


class _SuccessfulAdapterAgent:
    """让第二候选经同一真实 adapter 同时支持 completion 与文本流。"""

    async def run(self, _prompt: str, *, model_settings: object) -> _AdapterResult:
        del model_settings
        return _AdapterResult()

    @asynccontextmanager
    async def run_stream_events(self, _prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        del model_settings

        async def events():  # type: ignore[no-untyped-def]
            result = _AdapterResult()
            yield PartStartEvent(index=0, part=TextPart(content=result.output))
            yield AgentRunResultEvent(result=cast(Any, result))

        yield events()


class _PrimaryClientConstructionFailureFactory(ControlledOpenAIClientFactory):
    """首候选走真实factory失败分支，次候选只提供离线成功agent lease。"""

    def __init__(self) -> None:
        self.acquisitions: list[str] = []

        def fail_transport_construction() -> httpx.AsyncBaseTransport:
            self.acquisitions.append("transport_factory")
            raise RuntimeError("simulated transport construction failure")

        super().__init__(
            model_settings=chain_settings(route_count=2).model,
            transport_factory=fail_transport_construction,
        )

    async def acquire(self, plan: ModelRoutePlan) -> Any:
        """A复用真实构造与清理逻辑；B避免网络并返回合法agent读面。"""

        self.acquisitions.append(plan.deployment_id)
        if plan.deployment_id == "real_primary":
            return await super().acquire(plan)
        return SimpleNamespace(agent=_SuccessfulAdapterAgent())


@pytest.mark.asyncio
async def test_same_kind_deployments_isolate_client_catalog_and_bulkhead_identity() -> None:
    """同 kind 的 A/B 仍必须形成两份 route、client lease、catalog 与 Bulkhead identity。"""

    settings = chain_settings(route_count=2)
    policy = downstream_chain_policy(route_count=2)
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=lambda _plan: AsyncAgentDouble(),
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    chain = router.plan_chain(
        ModelRequest(prompt="hello", max_output_tokens=8),
        agent_policy=policy,
    )
    factory = ControlledOpenAIClientFactory(
        model_settings=settings.model,
        transport_factory=lambda: httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        ),
    )
    try:
        route_a = chain.candidates[0].route
        route_b = chain.candidates[1].route
        lease_a = await factory.acquire(route_a)
        lease_b = await factory.acquire(route_b)

        assert route_a.deployment_id != route_b.deployment_id
        assert route_a.credential_ref != route_b.credential_ref
        assert route_a.endpoint_origin != route_b.endpoint_origin
        assert route_a.model_catalog_digest != route_b.model_catalog_digest
        assert route_a.bulkhead_policy is not route_b.bulkhead_policy
        assert lease_a is not lease_b
        assert factory.client_construction_count == 2
    finally:
        await factory.aclose()


@pytest.mark.asyncio
async def test_close_and_reload_do_not_mutate_an_existing_frozen_chain() -> None:
    """当前配置漂移只影响新链；旧 plan identity 保持逐值冻结，关闭后不能重取 client。"""

    settings = chain_settings(route_count=2)
    policy = downstream_chain_policy(route_count=2)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={
            "openai-compatible": PydanticAIModelProvider(
                provider_id="openai-compatible",
                agent_factory=lambda _plan: AsyncAgentDouble(),
            )
        },
        model_settings=settings.model,
    )
    chain = router.plan_chain(
        ModelRequest(prompt="hello", max_output_tokens=8),
        agent_policy=policy,
    )
    frozen_payload = chain.model_dump(mode="json")

    settings.model.deployments["real_secondary"].base_url = "https://reloaded.example.test/v1"
    settings.model.deployments["real_secondary"].credential_ref = "reloaded-key"

    assert chain.model_dump(mode="json") == frozen_payload
    await router.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await router.prepare(
            ModelRequest(prompt="hello", max_output_tokens=8),
            plan=chain.candidates[0].route,
        )


@pytest.mark.asyncio
async def test_exhausted_real_chain_never_falls_through_to_fake(tmp_path: Path) -> None:
    """两个真实候选安全耗尽只能返回 route-chain exhausted，fake sentinel 始终零调用。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["trusted_business_not_started"],
            ROUTE_B["deployment_id"]: ["client_not_started"],
        },
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await fixture.bound.complete(
                ModelRequest(prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert exc_info.value.code == "model.route_chain_exhausted"
        assert fixture.fake_provider.calls == 0
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_real_adapter_preserves_trusted_classifier_facts_for_chain_transfer(
    tmp_path: Path,
) -> None:
    """真实 adapter 的单次 attempt 包装不得吞掉 endpoint-bound 未开始事实。"""

    calls: list[str] = []

    class NotStartedAgent:
        async def run(self, _prompt: str, *, model_settings: object) -> object:
            del model_settings
            calls.append("real_primary")
            raise ModelProviderError(
                "model.provider_failed",
                status_code=429,
                completion_observed=False,
                side_effect_state="started",
            )

    secondary = AsyncAgentDouble()

    def agent_factory(plan: ModelRoutePlan) -> object:
        deployment_id = plan.deployment_id
        if deployment_id == "real_primary":
            return NotStartedAgent()
        calls.append("prepare:real_secondary")
        return secondary

    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=agent_factory,
    )
    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        provider_override=provider,
    )
    try:
        response = await fixture.bound.complete(
            ModelRequest(prompt="adapter chain", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.model == ROUTE_B["model_id"]
        assert calls == ["real_primary", "prepare:real_secondary"]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        proof = state.candidates[0].not_started_proofs[0]
        assert proof.reason == "trusted_business_not_started"
        assert proof.http_status == 429
        assert proof.classifier_ref == "trusted_response_header_not_started"
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "streaming"])
async def test_real_adapter_normalizes_client_not_started_proof_before_transfer(
    tmp_path: Path,
    mode: str,
) -> None:
    """真实 adapter 的 connect失败事实必须形成canonical proof并只调用次选一次。"""

    calls: list[str] = []

    def agent_factory(plan: ModelRoutePlan) -> object:
        if plan.deployment_id == "real_primary":
            return _ClientNotStartedAdapterAgent(calls)
        calls.append("prepare:real_secondary")
        return _SuccessfulAdapterAgent()

    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        agent_factory=agent_factory,
    )
    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        provider_override=provider,
    )
    request = ModelRequest(
        capability="text_stream" if mode == "streaming" else "text_completion",
        prompt="adapter client failure",
        max_output_tokens=8,
    )
    try:
        if mode == "streaming":
            response = await fixture.bound.stream(request, operation_key=fixture.operation_key)
        else:
            response = await fixture.bound.complete(request, operation_key=fixture.operation_key)

        assert response.model == ROUTE_B["model_id"]
        assert calls == ["real_primary", "prepare:real_secondary"]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        proof = state.candidates[0].not_started_proofs[0]
        assert proof.reason == "client_not_started"
        assert proof.completion_observed is None
        assert proof.request_sent is False
        assert proof.http_response_observed is False
    finally:
        await provider.aclose()
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "streaming"])
async def test_real_client_factory_construction_failure_transfers_before_send(
    tmp_path: Path,
    mode: str,
) -> None:
    """真实factory构造在client/send前确定失败时必须proof-close并进入次选。"""

    client_factory = _PrimaryClientConstructionFailureFactory()
    provider = PydanticAIModelProvider(
        provider_id="openai-compatible",
        client_factory=client_factory,
    )
    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        provider_override=provider,
    )
    request = ModelRequest(
        capability="text_stream" if mode == "streaming" else "text_completion",
        prompt="adapter factory failure",
        max_output_tokens=8,
    )
    try:
        if mode == "streaming":
            response = await fixture.bound.stream(request, operation_key=fixture.operation_key)
        else:
            response = await fixture.bound.complete(request, operation_key=fixture.operation_key)

        assert response.model == ROUTE_B["model_id"]
        assert client_factory.acquisitions == [
            "real_primary",
            "transport_factory",
            "real_secondary",
        ]
        assert client_factory.client_construction_count == 0
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        proof = state.candidates[0].not_started_proofs[0]
        assert proof.reason == "client_not_started"
        assert proof.completion_observed is None
        assert proof.request_sent is False
        assert proof.http_response_observed is False
    finally:
        await provider.aclose()
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_default_fake_local_route_remains_network_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受控多供应商回退不能改变默认 fake/local 的零网络边界。"""

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default fake route must not open a network connection")

    monkeypatch.setattr("socket.socket.connect", reject_network)
    router = ModelRouter(
        config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
        providers={"fake": FakeModelProvider()},
    )
    response = await router.route(
        ModelRequest(provider="fake", prompt="offline", max_output_tokens=2)
    )

    assert response.provider == "fake"
    assert response.output_text.startswith("fake:")
