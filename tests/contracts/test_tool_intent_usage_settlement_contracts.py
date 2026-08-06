"""Tool-intent model turn 复用既有 durable usage settlement 的公共合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    _router_and_policy,  # pyright: ignore[reportPrivateUsage]
    _tool_catalog,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.events import EventBus
from agent_harness.events.sinks.local_jsonl import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelAttemptEvidence,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ProviderToolIntentCandidate,
    StructuredProviderCandidate,
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    build_tool_catalog,
    model_route_operation_identity_digest,
    stable_usage_call_id,
)
from agent_harness.models.structured import compile_output_schema_definition
from agent_harness.models.tool_intent import ToolCatalogConflictError
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations


class _PreparedToolIntent:
    """显式脚本化的 in-process provider handle；send 计数即外部副作用计数。"""

    def __init__(self, provider: _ToolIntentProvider, plan: Any) -> None:
        self._provider = provider
        self._plan = plan

    async def send_tool_intent(self) -> object:
        """只在 durable mark 后增加计数并返回预设 candidate。"""

        self._provider.send_count += 1
        if self._provider.invalid:
            return object()
        if self._provider.cross_capability:
            schema = _tool_catalog().tools[0]
            return StructuredProviderCandidate(
                schema_identity=schema.input_schema.identity,
                provider="openai-compatible",
                model="fixture-text-1",
                candidate={"q": "weather"},
                attempts=[
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="started",
                        outcome="completed",
                        completion_observed=True,
                        input_tokens=11,
                        output_tokens=4,
                        cost_usd=0.0001,
                        cost_status="reported",
                        latency_ms=2,
                    )
                ],
            )
        if self._provider.final_text:
            return ModelResponse(
                provider="openai-compatible",
                model="fixture-text-1",
                output_text="done",
                decision=self._plan.decision,
                token_usage={"input_tokens": 5, "output_tokens": 2},
                attempts=[
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="started",
                        outcome="completed",
                        completion_observed=True,
                        input_tokens=5,
                        output_tokens=2,
                        cost_usd=0.0001,
                        cost_status="reported",
                        latency_ms=1,
                    )
                ],
            )
        schema = _tool_catalog().tools[0]
        return ProviderToolIntentCandidate(
            provider="openai-compatible",
            model="fixture-text-1",
            tool_name="search",
            arguments={"q": "weather"},
            tool_schema_ref=schema.input_schema_ref,
            tool_schema_version=schema.input_schema_version,
            tool_schema_digest=schema.input_schema_digest,
            attempts=[
                ModelAttemptEvidence(
                    attempt=1,
                    side_effect_state="started",
                    outcome="completed",
                    completion_observed=True,
                    input_tokens=7,
                    output_tokens=3,
                    cost_usd=0.0001,
                    cost_status="reported",
                    latency_ms=2,
                )
            ],
        )

    async def aclose(self) -> None:
        """测试 handle 没有外部资源。"""


class _ToolIntentProvider:
    """不注册工具 callback 的 provider-neutral tool-intent double。"""

    provider_id = "openai-compatible"
    tool_intent_observation_supported = True

    def __init__(
        self,
        *,
        invalid: bool = False,
        final_text: bool = False,
        cross_capability: bool = False,
    ) -> None:
        self.invalid = invalid
        self.final_text = final_text
        self.cross_capability = cross_capability
        self.prepare_count = 0
        self.send_count = 0

    async def prepare_tool_intent(self, request: object, *, plan: object, tool_catalog_json: bytes):
        """记录 client/permit 构造，断言只收到冻结 provider catalog bytes。"""

        del request
        self.prepare_count += 1
        assert tool_catalog_json.startswith(b'{"schema_version":"provider-tool-catalog-v1"')
        return _PreparedToolIntent(self, plan)


async def _run_id(storage: SQLAlchemyStorage, *, agent_id: str = "agent-a") -> str:
    """创建 usage outbox 所需的最小租户、会话与运行。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id=agent_id,
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id=agent_id,
                trace_id="trace-a",
            )
        )
        await uow.commit()
        return run.id


async def _fixture(
    tmp_path: Path,
    *,
    invalid: bool = False,
    final_text: bool = False,
    cross_capability: bool = False,
    catalog: object | None = None,
):
    """组装真实 SQLite/outbox 与无网络 provider，避免 mock 掩盖重复结算。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'tool-intent.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await _run_id(storage)
    provider = _ToolIntentProvider(
        invalid=invalid,
        final_text=final_text,
        cross_capability=cross_capability,
    )
    router, policy = _router_and_policy()
    cast(Any, router)._providers["openai-compatible"] = provider
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")

    async def resolve_trace(**_: object) -> str:
        """固定事件 trace，令断言只聚焦 usage lifecycle。"""

        return "trace-a"

    def resolve_catalog(_agent_id: str, selection: ToolCatalogSelection | None):
        """测试resolver复用生产selection规则，避免夹具吞掉扩权输入。"""

        resolved_catalog = catalog if catalog is not None else _tool_catalog()
        entry = cast(Any, resolved_catalog).tools[0]
        return build_tool_catalog(
            allowed_tools=(entry.name,),
            registry_descriptors=(
                ToolCatalogSourceDescriptor(
                    name=entry.name,
                    action=entry.action,
                    resource=entry.resource,
                    input_schema=entry.input_schema,
                    registry_ordinal=0,
                ),
            ),
            selection=selection,
        )

    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        agent_policy_resolver=lambda _: policy,
        tool_catalog_resolver=resolve_catalog,
    )
    bound = service.bind_execution(
        identity=IdentityContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            roles=["member"],
        ),
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="find weather",
        capability="tool_intent",
        max_output_tokens=8,
    )
    return storage, sink, provider, service, bound, request, run_id


def _oversized_tool_catalog():
    """构造合法但超过冻结 512-byte provider catalog cap 的单工具目录。"""

    schema = compile_output_schema_definition(
        {
            "type": "object",
            "description": "x" * 1000,
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
        schema_ref="oversized-search-input",
        version="v1",
    )
    return build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=(
            ToolCatalogSourceDescriptor(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=schema,
                registry_ordinal=0,
            ),
        ),
        selection=None,
    )


@pytest.mark.asyncio
async def test_tool_intent_success_and_exact_replay_settle_usage_once(tmp_path: Path) -> None:
    """重复 operation 必须重放同一 intent，不重建 client、不再次 send 或重复 final。"""

    storage, sink, provider, _service, bound, request, run_id = await _fixture(tmp_path)
    try:
        first = await bound.complete_tool_intent(request, operation_key="turn-1")
        replay = await bound.complete_tool_intent(request, operation_key="turn-1")
        events = await sink.read(run_id=run_id)
        assert first == replay
        assert first.kind == "tool_intent"
        assert provider.prepare_count == 1
        assert provider.send_count == 1
        assert (
            sum(
                event.event_type.value == "model.usage.updated"
                and event.payload is not None
                and event.payload.get("outcome") == "completed"
                for event in events
            )
            == 1
        )
        assert sum(event.event_type.value == "model.request.started" for event in events) == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_loop_balance_rejects_route_reservation_before_claim_or_provider(
    tmp_path: Path,
) -> None:
    """生产ModelInvocation必须在root reservation前套用loop剩余量。"""

    storage, _sink, provider, service, _bound, request, run_id = await _fixture(tmp_path)
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    usage_call_id = stable_usage_call_id(
        context=context,
        operation_key="loop-reservation",
    )
    try:
        with pytest.raises(Exception) as failure:
            await service.complete_tool_loop_turn(
                request,
                context=context,
                usage_call_id=usage_call_id,
                loop_id="a" * 64,
                turn_ordinal=1,
                operation_identity_digest=model_route_operation_identity_digest(
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    agent_id=context.agent_id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    operation_key="loop-reservation",
                ),
                tool_catalog=_tool_catalog(),
                actor=IdentityContext(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    session_id="session-a",
                    roles=["member"],
                ),
                loop_token_bound=1,
                loop_cost_bound=0.00001,
            )
        async with storage.uow() as uow:
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                )
        assert getattr(failure.value, "code", None) == "model.tool_loop_limit_exceeded"
        assert provider.prepare_count == provider.send_count == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_invalid_adapter_result_is_failed_once_and_never_replayed(tmp_path: Path) -> None:
    """适配失败也必须耐久结算；重复提交只重放稳定失败，不触发第二次 provider。"""

    storage, sink, provider, _service, bound, request, run_id = await _fixture(
        tmp_path,
        invalid=True,
    )
    try:
        for _ in range(2):
            with pytest.raises(Exception) as failure:
                await bound.complete_tool_intent(request, operation_key="turn-1")
            assert getattr(failure.value, "code", None) == "model.tool_intent_invalid"
        events = await sink.read(run_id=run_id)
        assert provider.prepare_count == 1
        assert provider.send_count == 1
        assert (
            sum(
                event.event_type.value == "model.usage.updated"
                and event.payload is not None
                and event.payload.get("outcome") == "failed"
                for event in events
            )
            == 1
        )
        assert sum(event.event_type.value == "model.request.started" for event in events) == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_cross_capability_candidate_is_rejected_but_keeps_observed_usage(
    tmp_path: Path,
) -> None:
    """Structured candidate 不能冒充 tool result，已发生的 provider usage 仍须结算。"""

    storage, _sink, provider, _service, bound, request, _run_id_value = await _fixture(
        tmp_path,
        cross_capability=True,
    )
    try:
        with pytest.raises(Exception) as failure:
            await bound.complete_tool_intent(request, operation_key="turn-1")
        assert getattr(failure.value, "code", None) == "model.tool_intent_invalid"
        async with storage.uow() as uow:
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=stable_usage_call_id(
                    context=UsageEvidenceContext(
                        tenant_id="tenant-a",
                        run_id=_run_id_value,
                        agent_id="agent-a",
                        request_id="request-a",
                        trace_id="trace-a",
                    ),
                    operation_key="turn-1",
                ),
            )
            result_json = usage.result_json
        assert result_json is not None
        attempt = result_json["evidence"]["decision"]["attempts"][0]
        assert attempt["input_tokens"] == 11
        assert attempt["output_tokens"] == 4
        assert provider.prepare_count == provider.send_count == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_oversized_tool_catalog_rejects_before_usage_claim_and_provider(
    tmp_path: Path,
) -> None:
    """合法 schema 组合超出冻结 cap 时，planning 必须保持零 claim/client/send。"""

    storage, _sink, provider, _service, bound, request, run_id = await _fixture(
        tmp_path,
        catalog=_oversized_tool_catalog(),
    )
    try:
        with pytest.raises(Exception) as failure:
            await bound.complete_tool_intent(request, operation_key="turn-1")
        assert getattr(failure.value, "code", None) == "model.tool_catalog_conflict"
        async with storage.uow() as uow:
            usage_rows = [
                row
                for row in await uow.evidence_outbox.pending(run_id=run_id)
                if row.operation_kind == "model_usage"
            ]
        assert usage_rows == []
        assert provider.prepare_count == provider.send_count == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_protocol_final_text_uses_same_single_settlement(tmp_path: Path) -> None:
    """同一 tool-enabled protocol 的最终回答分支也只写一组 usage evidence。"""

    storage, sink, provider, _service, bound, request, run_id = await _fixture(
        tmp_path,
        final_text=True,
    )
    try:
        result = await bound.complete_tool_intent(request, operation_key="turn-1")
        replay = await bound.complete_tool_intent(request, operation_key="turn-1")
        events = await sink.read(run_id=run_id)
        assert result == replay
        assert result.kind == "final_text"
        assert result.response.output_text == "done"
        assert provider.prepare_count == provider.send_count == 1
        assert sum(event.event_type.value == "model.usage.updated" for event in events) == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_invalid_bound_catalog_selection_precedes_claim_client_and_provider(
    tmp_path: Path,
) -> None:
    """独立bound selection扩权必须在usage claim、prepare与send前关闭失败。"""

    storage, _sink, provider, _service, bound, request, run_id = await _fixture(tmp_path)
    try:
        with pytest.raises(ToolCatalogConflictError):
            await bound.complete_tool_intent(
                request,
                operation_key="turn-1",
                tool_selection=ToolCatalogSelection(tool_names=("unknown",)),
            )
        async with storage.uow() as uow:
            usage_rows = [
                row
                for row in await uow.evidence_outbox.pending(run_id=run_id)
                if row.operation_kind == "model_usage"
            ]
        assert usage_rows == []
        assert provider.prepare_count == provider.send_count == 0
    finally:
        await storage.dispose()
