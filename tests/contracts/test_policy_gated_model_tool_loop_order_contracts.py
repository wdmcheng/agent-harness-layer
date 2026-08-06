"""模型工具循环线性顺序与 Policy 短路合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.test_policy_gated_model_tool_loop_public_seam_contracts import (
    _bound_loop,
    _catalog,
    _FakeContextAssembly,
    _FakeToolRegistry,
    _model_loop_limits,
    _model_policy,
    _request,
    _ScriptedModelTurns,
    _ScriptStep,
)

from agent_harness.context import ContextAssemblyResult
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelRequest,
    ToolCatalog,
    ToolIntent,
)
from agent_harness.runtime import (
    BoundModelToolLoopService,
    ModelToolLoopError,
    ModelToolLoopService,
    build_execution_context,
)
from agent_harness.tools import ResolvedToolIntent, ToolCallResult, ToolError, ToolErrorCode


class _OrderedModelTurns(_ScriptedModelTurns):
    """在共享观察序列中标记每轮模型判别完成。"""

    def __init__(self, script: Sequence[_ScriptStep], steps: list[str]) -> None:
        super().__init__(script)
        self._steps = steps

    async def complete_tool_loop_turn(self, request: ModelRequest, **kwargs: object) -> object:
        result = await super().complete_tool_loop_turn(request, **kwargs)
        self._steps.append("model.turn")
        return result


class _OrderedToolRegistry(_FakeToolRegistry):
    """模拟 Registry 内部固定的 Policy/HITL/handler/output-guard 顺序。"""

    def __init__(self, steps: list[str], *, deny: bool = False) -> None:
        super().__init__()
        self._steps = steps
        self._deny = deny

    def resolve_intent(self, intent: ToolIntent, *, catalog: ToolCatalog) -> ResolvedToolIntent:
        result = super().resolve_intent(intent, catalog=catalog)
        self._steps.append("registry.resolve")
        return result

    async def call(self, request: ResolvedToolIntent, **kwargs: object) -> ToolCallResult:
        self._steps.extend(("policy.decision", "hitl.branch"))
        if self._deny:
            return ToolCallResult(
                tool_name=request.tool_name,
                status="denied",
                invocation_id="denied-before-handler",
                error=ToolError(
                    code=ToolErrorCode.POLICY_DENIED,
                    message="policy denied",
                ),
                source_ref=f"tool://{request.tool_call_id}",
            )
        self._steps.extend(("tool.execution", "output.guard"))
        return await super().call(request, **kwargs)


class _OrderedContextAssembly(_FakeContextAssembly):
    """只在成功工具结果之后记录 ContextAssembler 边界。"""

    def __init__(self, steps: list[str]) -> None:
        super().__init__()
        self._steps = steps

    async def assemble(self, **kwargs: object) -> ContextAssemblyResult:
        result = await super().assemble(**kwargs)
        self._steps.append("context.assembly")
        return result


def _ordered_bound_loop(
    *,
    deny: bool = False,
) -> tuple[BoundModelToolLoopService, list[str], _OrderedToolRegistry]:
    """共享安全阶段观察器，证明 runtime 和 Registry 的组合顺序。"""

    steps: list[str] = []
    model = _OrderedModelTurns(
        (_ScriptStep("tool_intent"), _ScriptStep("final_text")),
        steps,
    )
    registry = _OrderedToolRegistry(steps, deny=deny)
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: _catalog(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=_OrderedContextAssembly(steps),
        loop_limits_resolver=lambda _agent_id: _model_loop_limits(),
        agent_model_policy_resolver=lambda _agent_id: _model_policy(),
        step_observer=steps.append,
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id="ordered-tool-loop"),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)
    return bound, steps, registry


@pytest.mark.asyncio
async def test_loop_step_order_is_linear_and_content_free() -> None:
    """成功路径只能按 model→resolve→policy/HITL→tool/guard→context→model 推进。"""

    bound, steps, registry = _ordered_bound_loop()

    await bound.run(_request(), operation_key="ordered")

    assert steps == [
        "model.turn",
        "tool_intent.validated",
        "registry.resolve",
        "policy.decision",
        "hitl.branch",
        "tool.execution",
        "output.guard",
        "context.assembly",
        "model.turn",
        "final_text",
    ]
    assert registry.handler_count == 1


@pytest.mark.asyncio
async def test_policy_short_circuit_never_reaches_handler_context_or_next_model() -> None:
    """Registry 返回 deny 后，runtime 不得补做 handler、Context 或下一轮。"""

    bound, steps, registry = _ordered_bound_loop(deny=True)

    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(_request(), operation_key="denied")

    assert failure.value.code == ToolErrorCode.POLICY_DENIED.value
    assert steps == [
        "model.turn",
        "tool_intent.validated",
        "registry.resolve",
        "policy.decision",
        "hitl.branch",
    ]
    assert registry.handler_count == 0


@pytest.mark.asyncio
async def test_bound_loop_rejects_public_identity_injection_before_model() -> None:
    """tenant/run/agent/user/tool/policy身份没有公开输入面，注入尝试保持零副作用。"""

    bound, model, registry, _context = _bound_loop((_ScriptStep("final_text"),))

    with pytest.raises(TypeError):
        await cast(Any, bound).run(
            _request(),
            operation_key="identity",
            tenant_id="tenant-b",
            run_id="run-b",
            agent_id="agent-b",
            user_id="user-b",
            tool_name="other",
            policy_version="forged",
        )
    with pytest.raises(ValidationError):
        ModelRequest.model_validate(
            {
                **_request().model_dump(mode="python"),
                "tool_name": "other",
                "policy_version": "forged",
            }
        )
    assert model.calls == []
    assert registry.resolve_count == registry.handler_count == 0
