"""模型工具循环CanonicalEvent顺序、关联与容量合同。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    _tool_catalog,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_tool_intent_usage_settlement_contracts import (
    _fixture,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.context import ContextAssemblyService
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.events.model_tool_loop import (
    ModelToolLoopEventProducer,
)
from agent_harness.events.types import CanonicalEvent
from agent_harness.identity import IdentityContext
from agent_harness.models.tool_intent import tool_loop_identity_digest
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import AgentModelPolicy, AgentModelToolLoop
from agent_harness.runtime import (
    ModelToolLoopError,
    ModelToolLoopLimitOverrides,
    ModelToolLoopService,
)
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.tools import (
    BuiltinTool,
    ToolRegistry,
)


class _FailToolFinalSink(LocalJsonlEventSink):
    """模拟工具结果已耐久、但final event发布结果未知的本地sink。"""

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        """started正常持久化，工具终态在sink边界抛出不确定错误。"""

        if event.event_type in {
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.TOOL_CALL_FAILED,
        } and not getattr(self, "_failed_once", False):
            self._failed_once = True
            raise RuntimeError("tool final publish is unknown")
        return await super().write(event, after_claim=after_claim)


class _FailContextFinalSink(LocalJsonlEventSink):
    """模拟Context结果已耐久、但completed event发布结果未知的本地sink。"""

    async def write(
        self,
        event: CanonicalEvent,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> CanonicalEvent:
        if event.event_type == CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED and not getattr(
            self, "_failed_once", False
        ):
            self._failed_once = True
            raise RuntimeError("context final publish is unknown")
        return await super().write(event, after_claim=after_claim)


class _DelayedModelTurns:
    """让真实durable model turn跨过一秒deadline，稳定复现终态CAS边界。"""

    def __init__(self, inner: object, *, delay_seconds: float) -> None:
        self._inner = inner
        self._delay_seconds = delay_seconds

    async def complete_tool_loop_turn(self, request: object, **kwargs: object) -> object:
        """先完成fake provider与usage结算，再让wall clock越过冻结deadline。"""

        result = await cast(Any, self._inner).complete_tool_loop_turn(request, **kwargs)
        await asyncio.sleep(self._delay_seconds)
        return result

    async def read_tool_loop_turn_usage(self, **kwargs: object) -> object:
        """usage读取仍委托原生产测试seam，避免建立第二份结算夹具。"""

        return await cast(Any, self._inner).read_tool_loop_turn_usage(**kwargs)


def test_event_producer_uses_existing_typed_capacity_registry() -> None:
    """工具与Context步骤只能消费受信registry中的固定最大预约数。"""

    assert ModelToolLoopEventProducer is not None
    assert operation_event_capacity(EvidenceOperationKind.TOOL_INVOCATION) == 3
    assert operation_event_capacity(EvidenceOperationKind.CONTEXT_ASSEMBLY) == 2


async def _event_loop_fixture(
    tmp_path: Path,
    *,
    deny: bool = False,
    handler_failure: str | None = None,
    fail_final_publish: bool = False,
    fail_context_final_publish: bool = False,
    model_delay_seconds: float = 0.0,
):
    """组装真实ModelInvocation、Registry、SQLite/outbox与本地CanonicalEvent sink。"""

    storage, _model_sink, provider, model_turns, _bound, request, run_id = await _fixture(tmp_path)
    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    audit = AuditService(storage=storage)
    policy = PolicyEngine(
        provider=YamlPolicyProvider(
            deny_actions={"tool.search"} if deny else set(),
        ),
        audit=audit,
    )
    handler_count = 0

    def handler(arguments: dict[str, Any]) -> dict[str, object]:
        """唯一业务副作用计数器，并让下一模型轮返回最终文本。"""

        nonlocal handler_count
        handler_count += 1
        provider.final_text = True
        if handler_failure == "runtime":
            raise RuntimeError("handler failed with TOP_SECRET_OUTPUT")
        if handler_failure == "timeout":
            raise TimeoutError("handler timed out after side effect")
        if handler_failure == "cancelled":
            raise asyncio.CancelledError
        return {"result": arguments["q"], "private": "TOP_SECRET_OUTPUT"}

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
                input_schema_ref="search-input",
                input_schema_version="v1",
                handler=handler,
            )
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
        storage=storage,
    )

    async def resolve_trace(**_: object) -> str:
        """事件测试固定使用创建run时的可信trace。"""

        return "trace-a"

    sink_type = (
        _FailToolFinalSink
        if fail_final_publish
        else _FailContextFinalSink
        if fail_context_final_publish
        else LocalJsonlEventSink
    )
    sink = sink_type(tmp_path / "events.jsonl", run_trace_resolver=resolve_trace)
    event_bus = EventBus(
        sink=sink,
        artifact_store=artifact_store,
        capacity_storage=storage,
    )
    loop_events = ModelToolLoopEventProducer(storage=storage, event_bus=event_bus)
    runtime_model_turns = (
        _DelayedModelTurns(model_turns, delay_seconds=model_delay_seconds)
        if model_delay_seconds > 0
        else model_turns
    )
    loop_service = ModelToolLoopService(
        model_turns=runtime_model_turns,
        tool_catalog_resolver=lambda _agent_id, _selection: _tool_catalog(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=ContextAssemblyService(
            storage=storage,
            artifact_store=artifact_store,
        ),
        loop_limits_resolver=lambda _agent_id: AgentModelToolLoop(
            max_turns=4,
            # 路由先预约静态 1680-token 上界；循环上限必须容纳两轮预约，
            # 实际累计仍由每轮 durable usage 结算验证。
            max_total_tokens=4096,
            max_total_cost_usd=1.0,
            max_tool_output_bytes=8192,
            max_duration_seconds=60,
        ),
        agent_model_policy_resolver=lambda _agent_id: AgentModelPolicy(
            deployment_id="real_primary",
            provider="openai-compatible",
            allowed_models=["fixture-text-1"],
            default_model="fixture-text-1",
            fallback_models=[],
        ),
        loop_events=loop_events,
        storage=storage,
        artifact_store=artifact_store,
    )
    bound = loop_service.bind_execution(
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
    return (
        storage,
        sink,
        provider,
        bound,
        request,
        run_id,
        lambda: handler_count,
        registry,
        loop_events,
        model_turns,
    )


@pytest.mark.asyncio
async def test_full_loop_emits_linear_correlated_content_free_events(tmp_path: Path) -> None:
    """正常路径固定为model usage→tool→Context→下一model，事件不携带正文。"""

    (
        storage,
        sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    try:
        response = await bound.run(
            request.model_copy(update={"prompt": "TOP_SECRET_PROMPT"}),
            operation_key="event-order",
        )
        events = await sink.read(run_id=run_id)
        event_types = [event.event_type for event in events]

        assert response.output_text == "done"
        assert provider.send_count == 2
        assert handler_count() == 1
        assert event_types == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
            CanonicalEventType.TOOL_CALL_STARTED,
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.CONTEXT_ASSEMBLY_STARTED,
            CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED,
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        loop_events = events[2:6]
        assert all(event.payload is not None for event in loop_events)
        correlations = [
            cast(dict[str, Any], event.payload["correlation"])
            for event in loop_events
            if event.payload is not None
        ]
        assert all(correlation == correlations[0] for correlation in correlations)
        correlation = correlations[0]
        assert correlation["turn_ordinal"] == 1
        assert correlation["loop_id"]
        assert correlation["tool_call_id"]
        assert correlation["model_usage_call_id"]
        assert correlation["catalog_digest"] == _tool_catalog().catalog_digest
        model_events = (events[0], events[1], events[6], events[7])
        assert all(event.payload is not None for event in model_events)
        model_correlations = [
            cast(dict[str, Any], event.payload["correlation"])
            for event in model_events
            if event.payload is not None
        ]
        assert [item["turn_ordinal"] for item in model_correlations] == [1, 1, 2, 2]
        assert all(item["loop_id"] == correlation["loop_id"] for item in model_correlations)
        assert model_correlations[0]["tool_call_id"] is None
        assert model_correlations[1]["tool_call_id"] == correlation["tool_call_id"]
        assert model_correlations[2]["tool_call_id"] is None
        assert model_correlations[3]["tool_call_id"] is None
        serialized = json.dumps(
            [event.to_payload() for event in loop_events],
            ensure_ascii=False,
            sort_keys=True,
        )
        assert "TOP_SECRET_PROMPT" not in serialized
        assert "TOP_SECRET_OUTPUT" not in serialized
        assert '"q": "weather"' not in serialized
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_failure", ["runtime", "timeout", "cancelled"])
async def test_unapproved_handler_unknown_fences_public_loop(
    tmp_path: Path,
    handler_failure: str,
) -> None:
    """handler已产生副作用再异常时，公共普通入口必须关闭到needs-review。"""

    (
        storage,
        sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path, handler_failure=handler_failure)
    operation_key = "handler-outcome-unknown"
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    try:
        if handler_failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await bound.run(request, operation_key=operation_key)
        else:
            with pytest.raises(ModelToolLoopError) as failure:
                await bound.run(request, operation_key=operation_key)
            assert failure.value.code == "model.tool_loop_needs_review"
        assert provider.send_count == 1
        assert handler_count() == 1
        events = await sink.read(run_id=run_id)
        started = next(
            event for event in events if event.event_type == CanonicalEventType.TOOL_CALL_STARTED
        )
        assert started.payload is not None
        tool_call_id = cast(dict[str, Any], started.payload["correlation"])["tool_call_id"]
        assert isinstance(tool_call_id, str)
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.get("tenant-a", loop_id)
            claim = await uow.tool_invocations.get_by_tool_call_id(tool_call_id)
        assert loop is not None and loop.status == "needs_review"
        assert claim is not None and claim.execution_state == "needs_review"
        assert claim.result_ref is None
        assert not {
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.TOOL_CALL_FAILED,
        }.intersection(event.event_type for event in events)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_completed_loop_replays_exact_response_without_new_side_effects(
    tmp_path: Path,
) -> None:
    """相同公开run重放completed loop时复用耐久响应，不重调model、tool或Context。"""

    (
        storage,
        _sink,
        provider,
        bound,
        request,
        _run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    try:
        first = await bound.run(request, operation_key="completed-exact-replay")
        model_calls = provider.send_count
        tool_calls = handler_count()

        replayed = await bound.run(request, operation_key="completed-exact-replay")

        assert replayed == first
        assert provider.send_count == model_calls
        assert handler_count() == tool_calls
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_completed_loop_rejects_changed_effective_limits_before_side_effects(
    tmp_path: Path,
) -> None:
    """同一operation改写任一有效上限必须conflict，不能借旧bounds返回completed。"""

    (
        storage,
        _sink,
        provider,
        bound,
        request,
        _run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    try:
        await bound.run(request, operation_key="completed-bounds-conflict")
        model_calls = provider.send_count
        tool_calls = handler_count()

        with pytest.raises(ModelToolLoopError) as failure:
            await bound.run(
                request,
                operation_key="completed-bounds-conflict",
                limits=ModelToolLoopLimitOverrides(
                    max_turns=3,
                    max_total_tokens=None,
                    max_total_cost_usd=None,
                    max_tool_output_bytes=None,
                    max_duration_seconds=None,
                ),
            )

        assert failure.value.code == "model.tool_loop_replay_conflict"
        assert provider.send_count == model_calls
        assert handler_count() == tool_calls
    finally:
        await storage.dispose()
