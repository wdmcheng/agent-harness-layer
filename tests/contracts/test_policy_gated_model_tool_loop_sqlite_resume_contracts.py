"""模型工具循环经真实 SQLite/ApprovalService 跨进程审批续跑合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import update
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.test_policy_gated_model_tool_loop_event_contracts import (
    _FailToolFinalSink,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    _router_and_policy,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_tool_intent_usage_settlement_contracts import (
    _ToolIntentProvider,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.context import ContextAssemblyService
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.events.model_tool_loop import (
    ModelToolLoopEventProducer,
)
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelInvocationService,
    ModelRequest,
    ToolCatalog,
    ToolCatalogConflictError,
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    build_tool_catalog,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import AgentModelToolLoop
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    BoundModelToolLoopService,
    InvalidRunTransition,
    ModelToolLoopApprovalRequired,
    ModelToolLoopApprovalStore,
    ModelToolLoopService,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.models import CheckpointModel
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver
from agent_harness.tools import BuiltinTool, ToolRegistry

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
    "additionalProperties": False,
}


def _durable_request() -> ModelRequest:
    """跨进程合同使用真实tool-intent route身份，provider仍为进程内fake。"""

    return ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="use search",
        capability="tool_intent",
        max_output_tokens=8,
    )


class _ModelToolLoopExecutor:
    """把公开 loop waiting/resume 映射到既有 typed executor 协议。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """首次工具意图只创建 waiting approval，不捕获或执行 handler。"""

        del request
        loop = cast(
            BoundModelToolLoopService,
            context.require_service("model_tool_loop"),
        )
        try:
            response = await loop.run(
                _durable_request(),
                operation_key="durable-approval",
            )
        except ModelToolLoopApprovalRequired as waiting:
            return AgentExecutionResult.waiting(waiting.approval)
        return AgentExecutionResult.completed({"text": response.output_text})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """只用 active grant 恢复原 snapshot，禁止重新发送等待前的模型轮。"""

        del request
        loop = cast(
            BoundModelToolLoopService,
            context.require_service("model_tool_loop"),
        )
        response = await loop.resume(
            _durable_request(),
            operation_key="durable-approval",
            grant=grant,
        )
        return AgentExecutionResult.completed({"text": response.output_text})


def _build_runtime(
    *,
    storage: SQLAlchemyStorage,
    tmp_path: Path,
    identity: IdentityContext,
    final_text: bool,
    handler_effects: list[dict[str, Any]],
    handler_failure: str | None = None,
    fail_tool_final_publish: bool = False,
    registry_sink: list[ToolRegistry] | None = None,
    preflight_effects: list[dict[str, Any]] | None = None,
    loop_catalog_state: list[ToolCatalog] | None = None,
) -> tuple[ApprovalService, RunOrchestrator, _ToolIntentProvider]:
    """重建全部进程内协作者；SQLite、artifact 和 run 是唯一跨重载状态。"""

    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    audit = AuditService(storage=storage)
    policy = PolicyEngine(
        provider=YamlPolicyProvider(require_approval_actions={"tool.search"}),
        audit=audit,
    )

    def handler(arguments: dict[str, Any]) -> dict[str, str]:
        """唯一受控副作用计数器，用于证明 approve 前零执行且总共至多一次。"""

        handler_effects.append(dict(arguments))
        if handler_failure == "runtime":
            raise RuntimeError("deterministic approved handler failure")
        if handler_failure == "timeout":
            raise TimeoutError("approved handler timed out after side effect")
        if handler_failure == "cancelled":
            raise asyncio.CancelledError
        return {"value": str(arguments["q"])}

    def preflight(arguments: dict[str, Any]) -> None:
        """可选计数器证明恢复重验失败不会进入工具预检边界。"""

        if preflight_effects is not None:
            preflight_effects.append(dict(arguments))

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=_INPUT_SCHEMA,
                input_schema_ref="search-input",
                input_schema_version="v1",
                handler=handler,
                preflight=preflight,
            )
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
        storage=storage,
    )
    if registry_sink is not None:
        registry_sink.append(registry)
    catalog: ToolCatalog = build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=registry.catalog_descriptors(),
        selection=None,
    )
    if loop_catalog_state is not None:
        loop_catalog_state.append(catalog)
    event_sink_type = _FailToolFinalSink if fail_tool_final_publish else LocalJsonlEventSink
    event_bus = EventBus(
        sink=event_sink_type(tmp_path / "events.jsonl"),
        artifact_store=artifact_store,
        capacity_storage=storage,
        run_trace_resolver=StorageRunTraceResolver(storage),
    )
    provider = _ToolIntentProvider(final_text=final_text)
    router, invocation_policy = _router_and_policy()
    cast(Any, router)._providers["openai-compatible"] = provider

    def resolve_model_catalog(
        _agent_id: str,
        selection: ToolCatalogSelection | None,
    ) -> ToolCatalog:
        """让真实ModelInvocation与Registry消费同一冻结目录和selection。"""

        entry = catalog.tools[0]
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

    model_turns = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=event_bus,
        agent_policy_resolver=lambda _agent_id: invocation_policy,
        tool_catalog_resolver=resolve_model_catalog,
    )

    def resolve_loop_catalog(
        _agent_id: str,
        _selection: ToolCatalogSelection | None,
    ) -> ToolCatalog:
        """模拟生产Catalog resolver：加载后事实漂移时只抛稳定冲突。"""

        current = catalog if loop_catalog_state is None else loop_catalog_state[-1]
        if current != catalog:
            raise ToolCatalogConflictError
        return current

    loop_service = ModelToolLoopService(
        model_turns=model_turns,
        tool_catalog_resolver=resolve_loop_catalog,
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=ContextAssemblyService(
            storage=storage,
            artifact_store=artifact_store,
        ),
        loop_limits_resolver=lambda _agent_id: AgentModelToolLoop(
            max_turns=4,
            max_total_tokens=4096,
            max_total_cost_usd=1.0,
            max_tool_output_bytes=8192,
            max_duration_seconds=60,
        ),
        agent_model_policy_resolver=lambda _agent_id: invocation_policy,
        approval_store=ModelToolLoopApprovalStore(
            storage=storage,
            artifact_store=artifact_store,
        ),
        loop_events=ModelToolLoopEventProducer(storage=storage, event_bus=event_bus),
        storage=storage,
        artifact_store=artifact_store,
    )
    executor = _ModelToolLoopExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=identity,
        executor_resolver=lambda _agent_id: executor,
        executor_services={"model_tool_loop": loop_service},
    )
    approvals = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
    )
    return approvals, orchestrator, provider


@pytest.mark.asyncio
async def test_sqlite_reload_resumes_exact_snapshot_and_executes_tool_at_most_once(
    tmp_path: Path,
) -> None:
    """waiting 后关闭旧 storage；新进程从 artifact/lease 恢复且不重发首轮模型。"""

    dsn = sqlite_dsn(tmp_path / "model-tool-loop.db")
    run_migrations(dsn)
    await assert_database_reload_resumes_exact_snapshot(
        dsn=dsn,
        tmp_path=tmp_path,
    )


@pytest.mark.asyncio
async def test_sqlite_checkpoint_session_drift_blocks_resume_before_side_effects(
    tmp_path: Path,
) -> None:
    """真实checkpoint的session绑定漂移时，不发布恢复副作用也不调用provider或工具。"""

    dsn = sqlite_dsn(tmp_path / "model-tool-loop-checkpoint-session.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    identity = IdentityContext.local_default(session_id="checkpoint-session-owner")
    handler_effects: list[dict[str, Any]] = []
    approvals, orchestrator, provider = _build_runtime(
        storage=storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=handler_effects,
    )
    try:
        waiting = await orchestrator.start_run(
            agent_id="agent-a",
            input={"prompt": "use search"},
        )
        records = await approvals.list_for_run(actor=identity, run_id=waiting.run_id)
        assert len(records) == 1
        approval = records[0]
        async with storage.uow() as uow:
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=waiting.run_id,
                tenant_id=identity.tenant_id,
                request_id="checkpoint-session-request",
            )
            resume_token = waiting.resume_token
            assert resume_token is not None
            checkpoint = await uow.checkpoints.get_by_resume_token(resume_token.value)
            assert checkpoint is not None
            drifted_state = dict(checkpoint.state)
            drifted_state["session_id"] = "cross-session-checkpoint"
            await uow.session.execute(
                update(CheckpointModel)
                .where(CheckpointModel.id == checkpoint.id)
                .values(state_json=drifted_state)
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=approval.tenant_id,
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=str(approval.metadata["arguments_hash"]),
        )

        with pytest.raises(InvalidRunTransition, match="session_id"):
            await orchestrator.resume_run(
                waiting.resume_token or "",
                expected_run_id=waiting.run_id,
                identity=identity,
                approval_grant=grant,
            )
        assert handler_effects == []
        assert provider.send_count == 1
    finally:
        await storage.dispose()


async def assert_database_reload_resumes_exact_snapshot(
    *,
    dsn: str,
    tmp_path: Path,
) -> None:
    """在给定真实数据库上验证跨进程审批恢复、上下文重建与至多一次执行。"""

    identity = IdentityContext.local_default(session_id="model-tool-loop-sqlite")
    handler_effects: list[dict[str, Any]] = []

    first_storage = SQLAlchemyStorage.from_dsn(dsn)
    first_approvals, first_orchestrator, first_provider = _build_runtime(
        storage=first_storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=handler_effects,
    )
    waiting = await first_orchestrator.start_run(
        agent_id="agent-a",
        input={"prompt": "use search"},
    )
    async with first_storage.uow() as uow:
        waiting_run = await uow.runs.get(waiting.run_id)
    records = await first_approvals.list_for_run(actor=identity, run_id=waiting.run_id)
    assert waiting.status == RunStatus.WAITING, None if waiting_run is None else waiting_run.error
    assert len(records) == 1 and records[0].status == "waiting"
    assert handler_effects == []
    assert first_provider.send_count == 1
    continuation = cast(dict[str, Any], records[0].metadata["continuation"])
    async with first_storage.uow() as uow:
        waiting_loop = await uow.model_tool_loops.get("default", continuation["loop_id"])
    assert waiting_loop is not None and waiting_loop.status == "waiting_approval"
    assert waiting_loop.cumulative_usage.turns_completed == 1
    assert waiting_loop.cumulative_usage.total_tokens_used == 10
    assert waiting_loop.state.next_step == "approval_resume"
    assert waiting_loop.state.model_usage_call_id is not None
    expected_correlation = {
        "loop_id": continuation["loop_id"],
        "turn_ordinal": continuation["turn_ordinal"],
        "tool_call_id": continuation["tool_call_id"],
        "catalog_digest": continuation["catalog_digest"],
    }
    waiting_events = await LocalJsonlEventSink(tmp_path / "events.jsonl").read(
        run_id=waiting.run_id
    )
    required = next(
        event
        for event in waiting_events
        if event.event_type == CanonicalEventType.APPROVAL_REQUIRED
    )
    assert required.payload is not None
    assert required.payload["correlation"] == expected_correlation
    approval_id = records[0].approval_id
    await first_storage.dispose()

    resumed_storage = SQLAlchemyStorage.from_dsn(dsn)
    resumed_approvals, _, resumed_provider = _build_runtime(
        storage=resumed_storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=True,
        handler_effects=handler_effects,
    )
    try:
        resolved = await resumed_approvals.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval_id,
        )
        async with resumed_storage.uow() as uow:
            persisted_run = await uow.runs.get(waiting.run_id)
        assert resolved.approval.status == "approved"
        assert resolved.run is not None and resolved.run.status == RunStatus.COMPLETED, (
            None if persisted_run is None else persisted_run.error
        )
        assert handler_effects == [{"q": "weather"}]
        assert resumed_provider.send_count == 1
        async with resumed_storage.uow() as uow:
            tool_claim = await uow.tool_invocations.get_by_tool_call_id(
                expected_correlation["tool_call_id"]
            )
            approval_claim = await uow.tool_invocations.get_by_approval_id(approval_id)
        assert tool_claim is not None and approval_claim is not None
        assert tool_claim.id == approval_claim.id
        assert tool_claim.execution_state == "completed"
        assert tool_claim.handler_started_at is not None
        assert tool_claim.result_ref is not None
        async with resumed_storage.uow() as uow:
            completed_loop = await uow.model_tool_loops.get(
                "default", expected_correlation["loop_id"]
            )
        assert completed_loop is not None
        assert completed_loop.status == "completed"
        assert completed_loop.next_turn_ordinal == 3
        assert completed_loop.cumulative_usage.model_dump(mode="json") == {
            "schema_version": "model-tool-loop-cumulative-usage-v1",
            "turns_completed": 2,
            "total_tokens_used": 17,
            "total_cost_usd": 0.0002,
        }
        state = completed_loop.state.model_dump(mode="json")
        assert state == {
            "schema_version": "model-tool-loop-state-v1",
            "next_step": "terminal",
            "model_usage_call_id": state["model_usage_call_id"],
            "tool_call_id": expected_correlation["tool_call_id"],
            "approval_id": approval_id,
            "checkpoint_ref": state["checkpoint_ref"],
            "context_ref": state["context_ref"],
            "next_request_digest": None,
        }
        assert isinstance(state["model_usage_call_id"], str)
        assert isinstance(state["checkpoint_ref"], str)
        assert isinstance(state["context_ref"], str)
        resumed_events = await LocalJsonlEventSink(tmp_path / "events.jsonl").read(
            run_id=waiting.run_id
        )
        resolved_event = next(
            event
            for event in resumed_events
            if event.event_type == CanonicalEventType.APPROVAL_RESOLVED
        )
        assert resolved_event.payload is not None
        assert resolved_event.payload["correlation"] == expected_correlation
        loop_events = [
            event
            for event in resumed_events
            if event.event_type
            in {
                CanonicalEventType.TOOL_CALL_STARTED,
                CanonicalEventType.TOOL_CALL_COMPLETED,
                CanonicalEventType.CONTEXT_ASSEMBLY_STARTED,
                CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED,
            }
        ]
        assert [event.event_type for event in loop_events] == [
            CanonicalEventType.TOOL_CALL_STARTED,
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.CONTEXT_ASSEMBLY_STARTED,
            CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED,
        ]
        assert resumed_events[-1].event_type == CanonicalEventType.RUN_COMPLETED
        assert resumed_events[-1].terminal is True
        assert resumed_events.index(loop_events[-1]) < len(resumed_events) - 1
        for event in loop_events:
            assert event.payload is not None
            correlation = cast(dict[str, Any], event.payload["correlation"])
            assert {key: correlation[key] for key in expected_correlation} == expected_correlation

        with pytest.raises(ApprovalStateConflict):
            await resumed_approvals.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval_id,
            )
        assert handler_effects == [{"q": "weather"}]
    finally:
        await resumed_storage.dispose()
