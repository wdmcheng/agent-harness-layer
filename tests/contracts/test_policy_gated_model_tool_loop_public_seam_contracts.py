"""受控模型工具循环公开绑定接缝的 red-first 合同。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import pytest

from agent_harness.context import ContextAssemblyResult, ContextFragment
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FinalStructuredTurnResult,
    FinalTextTurnResult,
    ModelDecision,
    ModelRequest,
    ModelResponse,
    ModelUsageEvidence,
    OutputSchemaIdentity,
    StructuredOutputResult,
    ToolCatalog,
    ToolCatalogSourceDescriptor,
    ToolIntent,
    ToolIntentTurnResult,
    UsageEvidenceContext,
    build_tool_catalog,
    compile_output_schema_definition,
    structured_digest,
)
from agent_harness.registry import AgentModelPolicy, AgentModelToolLoop
from agent_harness.runtime import (
    BoundModelToolLoopService,
    ModelToolLoopError,
    ModelToolLoopService,
    build_execution_context,
)
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.tools import ResolvedToolIntent, ToolCallResult

_SCHEMA = compile_output_schema_definition(
    {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    },
    schema_ref="tools.search.input",
    version="v1",
)


def _catalog() -> ToolCatalog:
    """构造不含 handler 的单工具冻结目录。"""

    return build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=(
            ToolCatalogSourceDescriptor(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=_SCHEMA,
                registry_ordinal=0,
            ),
        ),
        selection=None,
    )


def _text_response(text: str = "done") -> ModelResponse:
    """构造 tool-intent protocol 可接受的最终文本。"""

    return ModelResponse(
        provider="fake",
        model="fake-tool-model",
        output_text=text,
        decision=ModelDecision(action="complete", estimated_tokens=2),
        token_usage={"input_tokens": 1, "output_tokens": 1},
    )


def _structured_response() -> ModelResponse:
    """构造必须被工具循环拒绝、但已完成单轮结算的结构化结果。"""

    identity = OutputSchemaIdentity(
        schema_ref="agents.example.Output",
        version="v1",
        digest="a" * 64,
    )
    return ModelResponse(
        provider="fake",
        model="fake-tool-model",
        output_text='{"answer":"done"}',
        decision=ModelDecision(action="complete", estimated_tokens=2),
        token_usage={"input_tokens": 1, "output_tokens": 1},
        structured_output=StructuredOutputResult(
            schema_identity=identity,
            value={"answer": "done"},
            repair_count=0,
            provider_request_count=1,
            replay_identity="b" * 64,
        ),
    )


@dataclass(frozen=True)
class _ScriptStep:
    """脚本只描述 provider-neutral 分支，不携带 callback 或执行能力。"""

    kind: Literal["final_text", "final_structured", "tool_intent", "native_execution"]


class _ScriptedModelTurns:
    """模拟已完成 usage 结算的内部单轮模型 seam。"""

    def __init__(
        self,
        script: Sequence[_ScriptStep],
        *,
        storage: SQLAlchemyStorage | None = None,
    ) -> None:
        self._script = tuple(script)
        self._storage = storage
        self.calls: list[tuple[int, str, str]] = []
        self.actor_users: list[str] = []

    async def complete_tool_loop_turn(self, request: ModelRequest, **kwargs: object) -> object:
        """按 runtime 派生的 ordinal 和冻结 catalog 返回下一 exact 分支。"""

        turn_ordinal = kwargs["turn_ordinal"]
        usage_call_id = kwargs["usage_call_id"]
        loop_id = kwargs["loop_id"]
        catalog = kwargs["tool_catalog"]
        actor = kwargs["actor"]
        assert isinstance(turn_ordinal, int)
        assert isinstance(usage_call_id, str)
        assert isinstance(loop_id, str)
        assert isinstance(catalog, ToolCatalog)
        assert isinstance(actor, IdentityContext)
        assert request.capability == "tool_intent"
        self.calls.append((turn_ordinal, usage_call_id, loop_id))
        self.actor_users.append(actor.user_id)
        context = kwargs["context"]
        assert isinstance(context, UsageEvidenceContext)
        await self._persist_usage_if_configured(
            context=context,
            usage_call_id=usage_call_id,
        )
        step = self._script[len(self.calls) - 1]
        if step.kind == "final_text":
            return FinalTextTurnResult(response=_text_response())
        if step.kind == "final_structured":
            return FinalStructuredTurnResult(response=_structured_response())
        if step.kind == "native_execution":
            return {
                "kind": "tool_intent",
                "provider_native_execution_count": 1,
                "handler": object(),
            }
        entry = catalog.tools[0]
        arguments = {"q": f"turn-{turn_ordinal}"}
        return ToolIntentTurnResult(
            intent=ToolIntent(
                loop_id=loop_id,
                turn_ordinal=turn_ordinal,
                tool_call_id=structured_digest(
                    {
                        "loop_id": loop_id,
                        "turn_ordinal": turn_ordinal,
                        "arguments": arguments,
                    }
                ),
                tool_name=entry.name,
                arguments=arguments,
                arguments_digest=structured_digest(arguments),
                tool_schema_ref=entry.input_schema_ref,
                tool_schema_version=entry.input_schema_version,
                tool_schema_digest=entry.input_schema_digest,
                model_usage_call_id=usage_call_id,
                catalog_digest=catalog.catalog_digest,
            )
        )

    async def _persist_usage_if_configured(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
    ) -> None:
        """为声明durable loop的隔离测试写入完整fake usage settlement。

        该夹具不伪造JSONL事件，只用于没有 EventBus 的组件合同；涉及事件顺序、
        crash recovery 或真实服务装配的测试必须继续使用生产 ModelInvocationService。
        """

        if self._storage is None:
            return
        evidence = await self.read_tool_loop_turn_usage(
            context=context,
            usage_call_id=usage_call_id,
            loop_id="fixture-loop-not-used",
            turn_ordinal=1,
        )
        reserved = operation_event_capacity(EvidenceOperationKind.MODEL_USAGE)
        async with self._storage.uow() as uow:
            existing = await uow.evidence_outbox.replay_usage(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                usage_call_id=usage_call_id,
                event_id=f"usage:{context.tenant_id}:{usage_call_id}:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            if existing is None:
                await uow.evidence_outbox.claim_usage(
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    usage_call_id=usage_call_id,
                    event_id=f"usage:{context.tenant_id}:{usage_call_id}:final",
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    started_evidence=evidence.to_payload(),
                )
                await uow.evidence_outbox.persist_result(
                    tenant_id=context.tenant_id,
                    usage_call_id=usage_call_id,
                    result={"evidence": evidence.to_payload(), "outcome": "completed"},
                )
                await uow.evidence_outbox.mark_published(
                    tenant_id=context.tenant_id,
                    usage_call_id=usage_call_id,
                )
                await uow.event_capacity.settle(
                    run_id=context.run_id,
                    reserved_event_count=reserved,
                    consumed=reserved,
                )
                await uow.commit()

    async def read_tool_loop_turn_usage(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> ModelUsageEvidence:
        """模拟已由durable settlement闭合的单轮actual usage。"""

        del loop_id, turn_ordinal

        return ModelUsageEvidence(
            usage_kind="model",
            tenant_id=context.tenant_id,
            provider="fake",
            model="fake-tool-model",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            cost_status="reported",
            latency_ms=1,
            decision={"usage_call_id": usage_call_id},
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )


class _FakeToolRegistry:
    """只计数 resolve/call；handler 副作用由 call 的单一计数表示。"""

    def __init__(self) -> None:
        self.resolve_count = 0
        self.handler_count = 0

    def resolve_intent(self, intent: ToolIntent, *, catalog: ToolCatalog) -> ResolvedToolIntent:
        """把已冻结意图投影为 data-only 解析结果。"""

        self.resolve_count += 1
        return ResolvedToolIntent(
            loop_id=intent.loop_id,
            turn_ordinal=intent.turn_ordinal,
            tool_call_id=intent.tool_call_id,
            tool_name=intent.tool_name,
            arguments=intent.arguments,
            arguments_digest=intent.arguments_digest,
            tool_schema_ref=intent.tool_schema_ref,
            tool_schema_version=intent.tool_schema_version,
            tool_schema_digest=intent.tool_schema_digest,
            model_usage_call_id=intent.model_usage_call_id,
            catalog_digest=intent.catalog_digest,
            action=catalog.tools[0].action,
            resource=catalog.tools[0].resource,
        )

    async def call(self, request: ResolvedToolIntent, **_: object) -> ToolCallResult:
        """模拟 Registry 已完成 Policy、handler 和 output guard 的成功结果。"""

        self.handler_count += 1
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed",
            invocation_id=f"invocation-{self.handler_count}",
            result={"value": f"result-{self.handler_count}"},
            source_ref=f"tool://{request.tool_call_id}",
            trust_level="untrusted",
            truncation={"truncated": False, "prompt_injection_signals": []},
        )


class _FakeContextAssembly:
    """验证每个工具结果先成为 untrusted fragment，再生成下一轮安全输入。"""

    def __init__(self) -> None:
        self.fragments: list[ContextFragment] = []

    async def assemble(self, **kwargs: object) -> ContextAssemblyResult:
        """复制唯一 fragment 并返回冻结 assembly ref。"""

        fragments = kwargs["fragments"]
        assert isinstance(fragments, list)
        typed_fragments = cast(list[object], fragments)
        assert len(typed_fragments) == 1
        fragment = typed_fragments[0]
        assert isinstance(fragment, ContextFragment)
        assert fragment.trust_level == "untrusted"
        self.fragments.append(fragment)
        index = len(self.fragments)
        return ContextAssemblyResult(
            id=f"assembly-{index}",
            output_ref=f"artifact://assembly-{index}",
            input_refs=[fragment.source_ref],
            token_budget=32,
            trust_summary={"untrusted": 1},
            truncation_summary={"used_tokens": fragment.token_estimate},
            assembled_text=fragment.content,
            retained_fragments=[fragment],
            fragment_traces=[],
        )


def _bound_loop(
    script: Sequence[_ScriptStep],
) -> tuple[BoundModelToolLoopService, _ScriptedModelTurns, _FakeToolRegistry, _FakeContextAssembly]:
    """通过正式 `build_execution_context` 取得业务可见的 run-bound façade。"""

    model = _ScriptedModelTurns(script)
    registry = _FakeToolRegistry()
    context_assembly = _FakeContextAssembly()
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: _catalog(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=context_assembly,
        loop_limits_resolver=lambda _agent_id: _model_loop_limits(),
        agent_model_policy_resolver=lambda _agent_id: _model_policy(),
    )
    identity = IdentityContext.local_default(session_id="tool-loop-contract")
    execution = build_execution_context(
        identity=identity,
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)
    return bound, model, registry, context_assembly


def _request() -> ModelRequest:
    """构造不携带 loop/turn/tool identity 的业务请求。"""

    return ModelRequest(
        provider="fake",
        model="fake-tool-model",
        prompt="answer with tools when needed",
        capability="tool_intent",
        max_output_tokens=16,
    )


def _model_policy() -> AgentModelPolicy:
    """冻结与公开请求逐值匹配的 Agent 模型授权。"""

    return AgentModelPolicy(
        deployment_id="fake_default",
        provider="fake",
        allowed_models=["fake-tool-model"],
        default_model="fake-tool-model",
        fallback_models=[],
    )


def _model_loop_limits() -> AgentModelToolLoop:
    """为公共loop seam提供完整且不依赖隐式默认的Agent maxima。"""

    return AgentModelToolLoop(
        max_turns=4,
        max_total_tokens=128,
        max_total_cost_usd=1.0,
        max_tool_output_bytes=8192,
        max_duration_seconds=60,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "expected_model_calls", "expected_tool_calls"),
    [
        ((_ScriptStep("final_text"),), 1, 0),
        ((_ScriptStep("tool_intent"), _ScriptStep("final_text")), 2, 1),
        (
            (
                _ScriptStep("tool_intent"),
                _ScriptStep("tool_intent"),
                _ScriptStep("final_text"),
            ),
            3,
            2,
        ),
    ],
)
async def test_bound_loop_owns_final_and_multi_tool_progression(
    script: Sequence[_ScriptStep],
    expected_model_calls: int,
    expected_tool_calls: int,
) -> None:
    """业务只提交operation，runtime派生连续turn并在唯一final_text结束。"""

    bound, model, registry, context_assembly = _bound_loop(script)

    response = await bound.run(_request(), operation_key="answer")

    assert response == _text_response()
    assert [ordinal for ordinal, _, _ in model.calls] == list(range(1, expected_model_calls + 1))
    assert len({loop_id for _, _, loop_id in model.calls}) == 1
    assert len({usage_call_id for _, usage_call_id, _ in model.calls}) == expected_model_calls
    assert registry.resolve_count == expected_tool_calls
    assert registry.handler_count == expected_tool_calls
    assert len(context_assembly.fragments) == expected_tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["final_structured", "native_execution"])
async def test_tool_loop_rejects_cross_capability_or_provider_native_execution(
    kind: Literal["final_structured", "native_execution"],
) -> None:
    """已结算的非法model分支不得变成structured成功或触发Registry/handler。"""

    bound, model, registry, context_assembly = _bound_loop((_ScriptStep(kind),))

    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(_request(), operation_key="answer")

    assert failure.value.code == "model.tool_intent_invalid"
    assert len(model.calls) == 1
    assert registry.resolve_count == registry.handler_count == 0
    assert context_assembly.fragments == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_request",
    [
        _request().model_copy(update={"provider": "other-provider"}),
        _request().model_copy(update={"model": "other-model"}),
        _request().model_copy(update={"deployment_id": "other-deployment"}),
    ],
)
async def test_agent_model_binding_drift_fails_before_model(
    model_request: ModelRequest,
) -> None:
    """请求只能缩小绑定 Agent route，不能改写 provider/model/deployment。"""

    bound, model, registry, _context = _bound_loop((_ScriptStep("final_text"),))

    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(model_request, operation_key="binding")

    assert failure.value.code == "model.tool_loop_replay_conflict"
    assert model.calls == []
    assert registry.resolve_count == registry.handler_count == 0


@pytest.mark.asyncio
async def test_bind_execution_copies_identity_and_rejects_tenant_drift() -> None:
    """composition错绑tenant被拒绝，绑定后修改原identity也不能改变provider actor。"""

    model = _ScriptedModelTurns((_ScriptStep("final_text"),))
    registry = _FakeToolRegistry()
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: _catalog(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=_FakeContextAssembly(),
        loop_limits_resolver=lambda _agent_id: _model_loop_limits(),
        agent_model_policy_resolver=lambda _agent_id: _model_policy(),
    )
    identity = IdentityContext.local_default(session_id="identity-snapshot")
    with pytest.raises(ModelToolLoopError):
        service.bind_execution(
            identity=identity,
            tenant_id="other-tenant",
            run_id="run-a",
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

    bound = service.bind_execution(
        identity=identity,
        tenant_id=identity.tenant_id,
        run_id="run-a",
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    original_user = identity.user_id
    identity.user_id = "mutated-after-bind"
    await bound.run(_request(), operation_key="identity-copy")

    assert model.actor_users == [original_user]


class _DriftingResolvedRegistry(_FakeToolRegistry):
    """返回与catalog action/resource不一致的解析结果，模拟可信边界漂移。"""

    def resolve_intent(self, intent: ToolIntent, *, catalog: ToolCatalog) -> ResolvedToolIntent:
        resolved = super().resolve_intent(intent, catalog=catalog)
        return resolved.model_copy(update={"action": "tool.other"})


@pytest.mark.asyncio
async def test_resolved_tool_binding_drift_precedes_policy_and_handler() -> None:
    """resolve DTO 即使由内部协作者改写，也不能进入 Registry execution seam。"""

    model = _ScriptedModelTurns((_ScriptStep("tool_intent"), _ScriptStep("final_text")))
    registry = _DriftingResolvedRegistry()
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: _catalog(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=_FakeContextAssembly(),
        loop_limits_resolver=lambda _agent_id: _model_loop_limits(),
        agent_model_policy_resolver=lambda _agent_id: _model_policy(),
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id="resolved-drift"),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = cast(BoundModelToolLoopService, execution.require_service("model_tool_loop"))

    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(_request(), operation_key="resolved-drift")

    assert failure.value.code == "model.tool_intent_invalid"
    assert registry.resolve_count == 1
    assert registry.handler_count == 0


# 后续Policy/HITL合同复用同一稳定fake seam；公开别名避免跨测试模块依赖私有符号。
FakeContextAssembly = _FakeContextAssembly
FakeToolRegistry = _FakeToolRegistry
ScriptedModelTurns = _ScriptedModelTurns
ScriptStep = _ScriptStep
model_loop_limits_fixture = _model_loop_limits
tool_catalog_fixture = _catalog
model_policy_fixture = _model_policy
tool_intent_request_fixture = _request
