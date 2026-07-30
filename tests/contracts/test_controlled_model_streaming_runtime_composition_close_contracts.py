"""活动 Pydantic stream 接入 service/provider composition close 的合同。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.config import load_settings
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.registry import AgentModelPolicy
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import stream_group_id


class _BlockingSDKContextAgent:
    """创建真实 SDK context 后阻塞 pull，供 composition close 确定性取消。"""

    def __init__(self) -> None:
        self.context_entered = asyncio.Event()
        self.pull_entered = asyncio.Event()
        self.context_calls = 0
        self.context_exits = 0
        self.pull_cancelled = False

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, model_settings: object):  # type: ignore[no-untyped-def]
        del prompt, model_settings
        self.context_calls += 1
        self.context_entered.set()

        async def events():  # type: ignore[no-untyped-def]
            self.pull_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.pull_cancelled = True
                raise
            if False:  # pragma: no cover - 保持 async generator 协议
                yield object()

        try:
            yield events()
        finally:
            self.context_exits += 1


class _BlockingClientFactory:
    """在 client acquisition 阶段阻塞，并记录关闭顺序。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.acquire_cancelled = asyncio.Event()
        self.order: list[str] = []

    async def acquire(self, plan: object) -> object:
        del plan
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.acquire_cancelled.set()
            self.order.append("acquire_cancelled")
            raise
        raise AssertionError("composition close must cancel client acquisition")

    async def aclose(self) -> None:
        self.order.append("factory_closed")


class _GatedCloseClientFactory:
    """把 provider close 暂停在 client factory，验证并发关闭等待同一事实。"""

    def __init__(self) -> None:
        self.close_entered = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.closed = False

    async def acquire(self, plan: object) -> object:
        del plan
        raise AssertionError("concurrent close contract must not prepare a client")

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.allow_close.wait()
        self.closed = True


class _FailingCloseClientFactory:
    """稳定制造 provider close 失败，验证 router 不缓存伪成功。"""

    async def acquire(self, plan: object) -> object:
        del plan
        raise AssertionError("close failure contract must not prepare a client")

    async def aclose(self) -> None:
        raise RuntimeError("factory close failed")


def _settings(
    *,
    max_in_flight: int = 2,
    queue_timeout_ms: int = 100,
) -> Any:
    """在受控真实 deployment 上启用 text_stream，不读取凭据或触网。"""

    overrides = real_model_override()
    model = cast(dict[str, Any], overrides["model"])
    deployments = cast(dict[str, dict[str, Any]], model["deployments"])
    deployment = deployments["real_primary"]
    deployment["capabilities"] = ["text_completion", "text_stream"]
    deployment["max_in_flight"] = max_in_flight
    deployment["queue_timeout_ms"] = queue_timeout_ms
    return load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def _policy() -> AgentModelPolicy:
    return AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )


def _request() -> ModelRequest:
    return ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        capability="text_stream",
        prompt="block until composition close",
        max_output_tokens=8,
    )


@pytest.mark.asyncio
async def test_service_close_cancels_active_pydantic_context_and_preserves_unknown_fence(
    tmp_path: Path,
) -> None:
    """service close 必须等待活动 pull/context 收口，并保留 started unknown 围栏。"""

    settings = _settings()
    agent = _BlockingSDKContextAgent()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-composition-close.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-composition-close.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    service_closed = False
    task: asyncio.Task[object] | None = None
    try:
        run_id = await seed_run(storage, request_id="request-composition-close")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-composition-close",
            trace_id="trace-a",
        )
        task = asyncio.create_task(
            bound.stream(_request(), operation_key="composition-close-active-stream")
        )
        await asyncio.wait_for(agent.pull_entered.wait(), timeout=2)

        await service.aclose()
        service_closed = True

        assert task.done()
        failure = task.exception()
        assert isinstance(failure, ModelProviderInvocationError)
        assert failure.code == "model.provider_side_effect_unknown"
        assert failure.provider_called is True
        assert agent.context_calls == agent.context_exits == 1
        assert agent.pull_cancelled is True

        events = await sink.read(run_id=run_id)
        started = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state

        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        assert group_states == ["started"] * 65
        assert usage_state == "needs_review"
        assert capacity.highest_persisted_seq == 1
        assert capacity.outstanding_reserved_event_count == 66

        # composition close 与显式 close 都必须幂等，不能重复退出 context 或释放 permit。
        await service.aclose()
        assert agent.context_exits == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not service_closed:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_service_close_cancels_pydantic_client_acquisition_before_factory_close(
    tmp_path: Path,
) -> None:
    """尚未形成 prepared stream 时，composition close 仍须等待 not-started 收口。"""

    settings = _settings()
    client_factory = _BlockingClientFactory()
    provider = PydanticAIModelProvider(client_factory=cast(Any, client_factory))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-client-acquisition-close.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-client-acquisition-close.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    service_closed = False
    task: asyncio.Task[object] | None = None
    try:
        run_id = await seed_run(storage, request_id="request-client-acquisition-close")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-client-acquisition-close",
            trace_id="trace-a",
        )
        task = asyncio.create_task(
            bound.stream(_request(), operation_key="client-acquisition-close")
        )
        await asyncio.wait_for(client_factory.entered.wait(), timeout=2)

        await service.aclose()
        service_closed = True

        assert task.done()
        failure = task.exception()
        assert isinstance(failure, ModelProviderInvocationError)
        assert failure.code == "model.invocation_cancelled"
        assert failure.provider_called is False
        assert client_factory.acquire_cancelled.is_set()
        assert client_factory.order == ["acquire_cancelled", "factory_closed"]

        events = await sink.read(run_id=run_id)
        started = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], started["correlation"])["usage_call_id"]
        final_payload = cast(dict[str, Any], events[1].payload)
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state

        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert final_payload["outcome"] == "cancelled"
        final_usage = cast(dict[str, Any], final_payload["usage"])
        assert cast(dict[str, Any], final_usage["decision"])["provider_called"] is False
        assert group_states == ["cancelled"] * 65
        assert usage_state == "published"
        assert capacity.highest_persisted_seq == 2
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not service_closed:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_service_close_cancels_permit_waiter_before_closing_client_factory(
    tmp_path: Path,
) -> None:
    """一个 prepare 持有 permit 时，等待 permit 的同 provider 调用也归 close 所有。"""

    settings = _settings(max_in_flight=1, queue_timeout_ms=2000)
    client_factory = _BlockingClientFactory()
    provider = PydanticAIModelProvider(client_factory=cast(Any, client_factory))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-permit-wait-close.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(
        tmp_path / "stream-permit-wait-close.jsonl",
        run_trace_resolver=resolve_trace,
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    service_closed = False
    tasks: list[asyncio.Task[object]] = []
    try:
        request_id = "request-permit-wait-close"
        run_id = await seed_run(storage, request_id=request_id)
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id=request_id,
            trace_id="trace-a",
        )
        for ordinal in (1, 2):
            tasks.append(
                asyncio.create_task(
                    bound.stream(_request(), operation_key=f"permit-wait-close-{ordinal}")
                )
            )
            if ordinal == 1:
                await asyncio.wait_for(client_factory.entered.wait(), timeout=2)

        # 第二个 started 已公开且尚未进入 client acquisition，证明它正等待唯一 permit。
        async with asyncio.timeout(2):
            while len(await sink.read(run_id=run_id)) < 2:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not tasks[1].done()

        await service.aclose()
        service_closed = True

        assert all(task.done() for task in tasks)
        failures = [task.exception() for task in tasks]
        assert all(isinstance(item, ModelProviderInvocationError) for item in failures)
        assert [cast(ModelProviderInvocationError, item).code for item in failures] == [
            "model.invocation_cancelled",
            "model.invocation_cancelled",
        ]
        assert all(
            cast(ModelProviderInvocationError, item).provider_called is False for item in failures
        )
        assert client_factory.order == ["acquire_cancelled", "factory_closed"]

        events = await sink.read(run_id=run_id)
        assert [event.event_type for event in events].count(
            CanonicalEventType.MODEL_REQUEST_STARTED
        ) == 2
        usage_events = [
            cast(dict[str, Any], event.payload)
            for event in events
            if event.event_type == CanonicalEventType.MODEL_USAGE_UPDATED
        ]
        assert len(usage_events) == 2
        assert all(item["outcome"] == "cancelled" for item in usage_events)
        assert all(
            cast(dict[str, Any], cast(dict[str, Any], item["usage"])["decision"])["provider_called"]
            is False
            for item in usage_events
        )
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not service_closed:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_concurrent_service_close_waits_for_shared_provider_completion(
    tmp_path: Path,
) -> None:
    """第二个 public close 必须等待首次 provider/client close 的完成事实。"""

    settings = _settings()
    client_factory = _GatedCloseClientFactory()
    provider = PydanticAIModelProvider(client_factory=cast(Any, client_factory))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-concurrent-close.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=LocalJsonlEventSink(
                tmp_path / "stream-concurrent-close.jsonl",
                run_trace_resolver=resolve_trace,
            ),
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _agent_id: _policy(),
    )
    close_tasks: list[asyncio.Task[None]] = []
    try:
        close_tasks.append(asyncio.create_task(service.aclose()))
        await asyncio.wait_for(client_factory.close_entered.wait(), timeout=2)
        close_tasks.append(asyncio.create_task(service.aclose()))
        await asyncio.sleep(0)

        assert not close_tasks[0].done()
        assert not close_tasks[1].done()
        assert client_factory.closed is False

        client_factory.allow_close.set()
        await asyncio.gather(*close_tasks)
        assert client_factory.closed is True
    finally:
        client_factory.allow_close.set()
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_router_close_failure_is_replayed_instead_of_cached_as_success() -> None:
    """首次 provider close 失败后，后续 close 必须继续显式失败。"""

    provider = PydanticAIModelProvider(client_factory=cast(Any, _FailingCloseClientFactory()))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=_settings().model,
    )

    with pytest.raises(RuntimeError, match="factory close failed"):
        await router.aclose()
    with pytest.raises(RuntimeError, match="model router close did not complete"):
        await router.aclose()


@pytest.mark.asyncio
async def test_cancelled_router_close_is_not_cached_as_success() -> None:
    """首次关闭者被取消时，后续 close 必须观察未完成而非直接返回。"""

    client_factory = _GatedCloseClientFactory()
    provider = PydanticAIModelProvider(client_factory=cast(Any, client_factory))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=_settings().model,
    )
    first_close = asyncio.create_task(router.aclose())
    await asyncio.wait_for(client_factory.close_entered.wait(), timeout=2)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    with pytest.raises(RuntimeError, match="model router close did not complete"):
        await router.aclose()
    assert client_factory.closed is False
