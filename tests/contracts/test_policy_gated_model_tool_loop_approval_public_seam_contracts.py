"""模型工具循环 waiting/resume 的公开审批接缝合同。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from tests.contracts.test_policy_gated_model_tool_loop_public_seam_contracts import (
    FakeContextAssembly,
    FakeToolRegistry,
    ScriptedModelTurns,
    ScriptStep,
    model_loop_limits_fixture,
    model_policy_fixture,
    tool_catalog_fixture,
    tool_intent_request_fixture,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ToolCatalog,
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    build_tool_catalog,
)
from agent_harness.registry import AgentModelToolLoop
from agent_harness.runtime import (
    ApprovalGrant,
    BoundModelToolLoopService,
    ModelToolLoopApprovalRequired,
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopError,
    ModelToolLoopLimitOverrides,
    ModelToolLoopService,
    build_execution_context,
)
from agent_harness.runtime.executor import AgentApprovalRequest
from agent_harness.tools import ResolvedToolIntent, ToolCallResult, ToolError, ToolErrorCode
from agent_harness.tools.approved_execution import hash_tool_arguments


class _ApprovalRegistry(FakeToolRegistry):
    """首次调用只返回waiting；matching grant路径才计数approved handler。"""

    def __init__(self) -> None:
        super().__init__()
        self.approved_count = 0

    async def call(self, request: ResolvedToolIntent, **_: object) -> ToolCallResult:
        return ToolCallResult(
            tool_name=request.tool_name,
            status="requires_approval",
            invocation_id="waiting-before-handler",
            error=ToolError(
                code=ToolErrorCode.APPROVAL_REQUIRED,
                message="approval required",
            ),
            source_ref=f"tool://{request.tool_call_id}",
            policy={
                "decision": "require_approval",
                "reason": "approval required",
                "action": request.action,
                "resource": request.resource,
            },
        )

    async def call_approved(
        self,
        request: ResolvedToolIntent,
        **_: object,
    ) -> ToolCallResult:
        self.approved_count += 1
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed",
            invocation_id="approved-once",
            result={"value": "approved"},
            source_ref=f"tool://{request.tool_call_id}",
            truncation={"truncated": False},
        )


class _ArtifactApprovalRegistry(_ApprovalRegistry):
    """批准后返回只含artifact引用的截断结果，验证冻结输出边界。"""

    async def call_approved(
        self,
        request: ResolvedToolIntent,
        **_: object,
    ) -> ToolCallResult:
        self.approved_count += 1
        artifact_ref = "artifact://" + "a" * 64
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed",
            invocation_id="approved-artifact",
            result={"artifact_ref": artifact_ref},
            source_ref=f"tool://{request.tool_call_id}",
            artifact_ref=artifact_ref,
            truncation={
                "truncated": True,
                "original_bytes": 4096,
                "inline_bytes": 64,
                "prompt_injection_signals": [],
            },
        )


class _GuardFailureRegistry(_ApprovalRegistry):
    """让普通或批准入口返回同一受守卫失败，验证后续控制流完全一致。"""

    def __init__(self, *, approval_first: bool) -> None:
        super().__init__()
        self.approval_first = approval_first

    @staticmethod
    def _failure(request: ResolvedToolIntent) -> ToolCallResult:
        """构造不含结果正文的稳定执行失败。"""

        return ToolCallResult(
            tool_name=request.tool_name,
            status="failed",
            invocation_id="guarded-failure",
            error=ToolError(
                code=ToolErrorCode.EXECUTION_FAILED,
                message="guarded failure",
            ),
            source_ref=f"tool://{request.tool_call_id}",
        )

    async def call(self, request: ResolvedToolIntent, **kwargs: object) -> ToolCallResult:
        if self.approval_first:
            return await super().call(request, **kwargs)
        return self._failure(request)

    async def call_approved(
        self,
        request: ResolvedToolIntent,
        **_: object,
    ) -> ToolCallResult:
        self.approved_count += 1
        return self._failure(request)


class _MemoryApprovalStore:
    """只保存exact snapshot；真实SQLite/ApprovalService由集成合同覆盖。"""

    def __init__(self) -> None:
        self.snapshot: ModelToolLoopApprovalSnapshot | None = None
        self.approval: AgentApprovalRequest | None = None
        self.failure_code: str | None = None

    def create(
        self,
        *,
        snapshot: ModelToolLoopApprovalSnapshot,
        reason: str,
    ) -> AgentApprovalRequest:
        self.snapshot = snapshot
        self.approval = AgentApprovalRequest(
            action=snapshot.action,
            resource=snapshot.resource,
            reason=reason,
            arguments_ref="artifact://" + "a" * 64,
            arguments_hash=hash_tool_arguments(snapshot.intent.arguments),
            continuation={
                "kind": "model_tool_loop",
                "snapshot_digest": snapshot.snapshot_digest,
            },
        )
        return self.approval

    async def resolve(
        self,
        *,
        grant: ApprovalGrant,
    ) -> ModelToolLoopApprovalSnapshot:
        if self.failure_code is not None:
            raise ModelToolLoopError(self.failure_code)
        assert self.snapshot is not None
        assert grant.arguments_hash == hash_tool_arguments(self.snapshot.intent.arguments)
        return self.snapshot


class _ApprovalClock:
    """审批等待测试专用的受信UTC时钟。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _selective_catalog(selection: ToolCatalogSelection | None) -> ToolCatalog:
    """构造双工具目录，使审批恢复必须保留首次显式保序子集。"""

    search = tool_catalog_fixture().tools[0]
    return build_tool_catalog(
        allowed_tools=("search", "lookup"),
        registry_descriptors=(
            ToolCatalogSourceDescriptor(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=search.input_schema,
                registry_ordinal=0,
            ),
            ToolCatalogSourceDescriptor(
                name="lookup",
                action="tool.lookup",
                resource="tool:lookup",
                input_schema=search.input_schema,
                registry_ordinal=1,
            ),
        ),
        selection=selection,
    )


def _bound_approval_loop(
    script: Sequence[ScriptStep],
    *,
    trusted_clock: _ApprovalClock | None = None,
    loop_limits: AgentModelToolLoop | None = None,
    approval_store: _MemoryApprovalStore | None = None,
    registry: _ApprovalRegistry | None = None,
    context_assembly: FakeContextAssembly | None = None,
    session_id: str = "tool-loop-approval",
) -> tuple[
    BoundModelToolLoopService,
    ScriptedModelTurns,
    _ApprovalRegistry,
    _MemoryApprovalStore,
]:
    """绑定fake model/tool与内存审批快照，隔离公开handoff行为。"""

    model = ScriptedModelTurns(script)
    registry = registry or _ApprovalRegistry()
    approval_store = approval_store or _MemoryApprovalStore()
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: tool_catalog_fixture(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=context_assembly or FakeContextAssembly(),
        loop_limits_resolver=lambda _agent_id: loop_limits or model_loop_limits_fixture(),
        agent_model_policy_resolver=lambda _agent_id: model_policy_fixture(),
        approval_store=approval_store,
        trusted_clock=trusted_clock,
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id=session_id),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)
    return bound, model, registry, approval_store


def _grant(snapshot: ModelToolLoopApprovalSnapshot) -> ApprovalGrant:
    """构造与waiting snapshot逐值匹配的active lease grant。"""

    return ApprovalGrant(
        approval_id="approval-a",
        lease_id="lease-a",
        tenant_id=snapshot.context.tenant_id,
        identity_id="local-user",
        session_id=snapshot.session_id,
        agent_id=snapshot.context.agent_id,
        run_id=snapshot.context.run_id,
        action=snapshot.action,
        resource=snapshot.resource,
        arguments_hash=hash_tool_arguments(snapshot.intent.arguments),
    )


@pytest.mark.asyncio
async def test_waiting_snapshot_resumes_through_call_approved_exactly_once() -> None:
    """首次waiting零handler；matching grant从原turn续跑且不重发首轮模型。"""

    bound, model, registry, store = _bound_approval_loop(
        (ScriptStep("tool_intent"), ScriptStep("final_text"))
    )

    with pytest.raises(ModelToolLoopApprovalRequired) as waiting:
        await bound.run(tool_intent_request_fixture(), operation_key="approval")

    assert waiting.value.approval == store.approval
    assert store.snapshot is not None
    assert registry.handler_count == registry.approved_count == 0
    response = await bound.resume(
        tool_intent_request_fixture(),
        operation_key="approval",
        grant=_grant(store.snapshot),
    )

    assert response.output_text == "done"
    assert [ordinal for ordinal, _, _ in model.calls] == [1, 2]
    assert registry.approved_count == 1


@pytest.mark.asyncio
async def test_waiting_snapshot_rejects_cross_session_resume_before_approved_handler() -> None:
    """同一用户的另一会话不能取得原审批快照所绑定的工具执行权。"""

    registry = _ApprovalRegistry()
    store = _MemoryApprovalStore()
    waiting_bound, _, _, _ = _bound_approval_loop(
        (ScriptStep("tool_intent"),),
        approval_store=store,
        registry=registry,
        session_id="session-a",
    )
    with pytest.raises(ModelToolLoopApprovalRequired):
        await waiting_bound.run(
            tool_intent_request_fixture(),
            operation_key="cross-session-approval",
        )
    assert store.snapshot is not None

    resumed_bound, resumed_model, _, _ = _bound_approval_loop(
        (ScriptStep("final_text"),),
        approval_store=store,
        registry=registry,
        session_id="session-b",
    )
    with pytest.raises(ModelToolLoopError) as failure:
        await resumed_bound.resume(
            tool_intent_request_fixture(),
            operation_key="cross-session-approval",
            grant=_grant(store.snapshot),
        )

    assert failure.value.code == "tool.approval_invalid"
    assert registry.approved_count == 0
    assert resumed_model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("over_limit", [False, True])
async def test_approval_artifact_reference_honors_exact_frozen_byte_limit(
    over_limit: bool,
) -> None:
    """批准恢复与普通入口共享artifact引用边界，超限时不进入Context或下一模型轮。"""

    artifact_content = '{"artifact_ref":"artifact://' + "a" * 64 + '"}'
    max_bytes = len(artifact_content.encode("utf-8")) - int(over_limit)
    registry = _ArtifactApprovalRegistry()
    assembly = FakeContextAssembly()
    bound, model, _, store = _bound_approval_loop(
        (ScriptStep("tool_intent"), ScriptStep("final_text")),
        registry=registry,
        context_assembly=assembly,
    )
    with pytest.raises(ModelToolLoopApprovalRequired):
        await bound.run(
            tool_intent_request_fixture(),
            operation_key=f"approval-artifact-{over_limit}",
            limits=ModelToolLoopLimitOverrides(
                max_turns=None,
                max_total_tokens=None,
                max_total_cost_usd=None,
                max_tool_output_bytes=max_bytes,
                max_duration_seconds=None,
            ),
        )
    assert store.snapshot is not None

    operation = bound.resume(
        tool_intent_request_fixture(),
        operation_key=f"approval-artifact-{over_limit}",
        grant=_grant(store.snapshot),
    )
    if over_limit:
        with pytest.raises(ModelToolLoopError) as failure:
            await operation
        assert failure.value.code == "model.tool_loop_limit_exceeded"
        assert assembly.fragments == []
        assert [ordinal for ordinal, _, _ in model.calls] == [1]
    else:
        response = await operation
        assert response.output_text == "done"
        assert [fragment.content for fragment in assembly.fragments] == [artifact_content]
        assert [ordinal for ordinal, _, _ in model.calls] == [1, 2]
    assert registry.approved_count == 1


@pytest.mark.asyncio
async def test_approval_resume_reuses_original_deadline_and_balance_before_handler() -> None:
    """等待不会重算deadline或清空已结算usage；过期恢复保持零handler。"""

    clock = _ApprovalClock()
    bound, model, _registry, store = _bound_approval_loop(
        (ScriptStep("tool_intent"), ScriptStep("final_text")),
        trusted_clock=clock,
    )
    limits = ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=10,
        max_total_cost_usd=None,
        max_tool_output_bytes=None,
        max_duration_seconds=1,
    )
    with pytest.raises(ModelToolLoopApprovalRequired):
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="approval-deadline",
            limits=limits,
        )
    assert store.snapshot is not None
    frozen_deadline = store.snapshot.limits.deadline_at
    frozen_tokens = store.snapshot.limits.total_tokens_used
    clock.advance(2)
    expanded = AgentModelToolLoop(
        max_turns=8,
        max_total_tokens=256,
        max_total_cost_usd=2.0,
        max_tool_output_bytes=16_384,
        max_duration_seconds=120,
    )
    resumed_bound, resumed_model, resumed_registry, resumed_store = _bound_approval_loop(
        (ScriptStep("final_text"),),
        trusted_clock=clock,
        loop_limits=expanded,
        approval_store=store,
    )

    with pytest.raises(ModelToolLoopError) as failure:
        await resumed_bound.resume(
            tool_intent_request_fixture(),
            operation_key="approval-deadline",
            grant=_grant(store.snapshot),
        )

    assert failure.value.code == "model.tool_loop_limit_exceeded"
    assert store.snapshot.limits.deadline_at == frozen_deadline
    assert store.snapshot.limits.total_tokens_used == frozen_tokens == 2
    assert len(model.calls) == 1
    assert resumed_model.calls == []
    assert resumed_registry.approved_count == 0
    assert resumed_store is store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "approval.expired",
        "approval.denied",
        "approval.revoked",
        "approval.resolution_in_progress",
    ],
)
async def test_non_active_approval_never_reaches_approved_handler(code: str) -> None:
    """过期、拒绝、撤销或lease竞争均在call_approved前关闭。"""

    bound, _, registry, store = _bound_approval_loop((ScriptStep("tool_intent"),))
    with pytest.raises(ModelToolLoopApprovalRequired):
        await bound.run(tool_intent_request_fixture(), operation_key="blocked-resume")
    assert store.snapshot is not None
    store.failure_code = code

    with pytest.raises(ModelToolLoopError) as failure:
        await bound.resume(
            tool_intent_request_fixture(),
            operation_key="blocked-resume",
            grant=_grant(store.snapshot),
        )

    assert failure.value.code == code
    assert registry.approved_count == 0


@pytest.mark.asyncio
async def test_resume_rebuilds_the_same_explicit_catalog_selection() -> None:
    """审批恢复重验当前Registry，但不能把首次显式子集扩大成全量目录。"""

    model = ScriptedModelTurns((ScriptStep("tool_intent"), ScriptStep("final_text")))
    registry = _ApprovalRegistry()
    store = _MemoryApprovalStore()
    seen_selections: list[tuple[str, ...] | None] = []

    def resolve_catalog(
        _agent_id: str,
        selection: ToolCatalogSelection | None,
    ) -> ToolCatalog:
        seen_selections.append(None if selection is None else selection.tool_names)
        return _selective_catalog(selection)

    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=resolve_catalog,
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=FakeContextAssembly(),
        loop_limits_resolver=lambda _agent_id: model_loop_limits_fixture(),
        agent_model_policy_resolver=lambda _agent_id: model_policy_fixture(),
        approval_store=store,
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id="tool-loop-selection"),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)
    selection = ToolCatalogSelection(tool_names=("search",))
    with pytest.raises(ModelToolLoopApprovalRequired):
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="approval-selection",
            tool_selection=selection,
        )
    assert store.snapshot is not None

    response = await bound.resume(
        tool_intent_request_fixture(),
        operation_key="approval-selection",
        grant=_grant(store.snapshot),
    )

    assert response.output_text == "done"
    assert seen_selections == [("search",), ("search",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "approved"])
async def test_normal_and_approved_entries_share_the_same_result_guard(mode: str) -> None:
    """两条入口的失败结果都在ContextAssembler和下一模型轮之前关闭。"""

    model = ScriptedModelTurns((ScriptStep("tool_intent"),))
    registry = _GuardFailureRegistry(approval_first=mode == "approved")
    store = _MemoryApprovalStore()
    context_assembly = FakeContextAssembly()
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: tool_catalog_fixture(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=context_assembly,
        loop_limits_resolver=lambda _agent_id: model_loop_limits_fixture(),
        agent_model_policy_resolver=lambda _agent_id: model_policy_fixture(),
        approval_store=store,
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id=f"tool-loop-guard-{mode}"),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)

    if mode == "normal":
        operation = bound.run(
            tool_intent_request_fixture(),
            operation_key="guard-normal",
        )
    else:
        with pytest.raises(ModelToolLoopApprovalRequired):
            await bound.run(
                tool_intent_request_fixture(),
                operation_key="guard-approved",
            )
        assert store.snapshot is not None
        operation = bound.resume(
            tool_intent_request_fixture(),
            operation_key="guard-approved",
            grant=_grant(store.snapshot),
        )

    with pytest.raises(ModelToolLoopError) as failure:
        await operation
    assert failure.value.code == ToolErrorCode.EXECUTION_FAILED.value
    assert context_assembly.fragments == []
    assert [ordinal for ordinal, _, _ in model.calls] == [1]
