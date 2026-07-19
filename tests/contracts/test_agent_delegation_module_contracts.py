"""`agent.delegate` 内置 module 的可信 run 绑定与 payload 边界合同。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_harness.delegation import (
    AgentDelegateInput,
    AgentDelegationModule,
    DelegationChildSummary,
    DelegationExecutionResult,
    DelegationRequest,
    DelegationSummary,
)
from agent_harness.delegation.service import DelegationService
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunDetailResult, RunStatus
from agent_harness.runtime.executor import build_execution_context
from app.api.routes.runs import get_run_with_orchestrator


def _identity(tenant_id: str = "tenant-a") -> IdentityContext:
    """构造带 delegation 权限的固定调用身份，便于各合同断言聚焦可信字段。"""

    return IdentityContext(
        tenant_id=tenant_id,
        user_id="user-a",
        session_id="session-a",
        roles=["operator"],
        permissions=["agent.delegate"],
        auth_method="api-key",
    )


class _RecordingService:
    """记录 module 下沉的请求与身份，不模拟真实 delegation 副作用。"""

    def __init__(self) -> None:
        """初始化为空记录，便于拒绝路径断言 service 从未被调用。"""

        self.request: DelegationRequest | None = None
        self.identity: IdentityContext | None = None

    async def delegate(
        self,
        request: DelegationRequest,
        *,
        identity: IdentityContext,
    ) -> DelegationExecutionResult:
        """保存受 module 绑定后的可信输入，并返回最小已 claim 结果。"""

        self.request = request
        self.identity = identity
        return DelegationExecutionResult(
            delegation_id="delegation-a",
            parent_run_id=request.parent_run_id,
            child_run_id="child-a",
            status="claimed",
            summary=None,
        )


@pytest.mark.asyncio
async def test_runtime_binds_parent_source_and_request_before_business_payload() -> None:
    """验证执行上下文先注入 parent/source/request，再交给业务 payload 组装。"""

    recorder = _RecordingService()
    context = build_execution_context(
        identity=_identity(),
        services={
            "agent.delegate": AgentDelegationModule(cast(DelegationService, recorder)),
        },
        agent_id="agent-source",
        run_id="parent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    module = cast(Any, context.require_service("agent.delegate"))

    result = await module.delegate(
        AgentDelegateInput(
            target_agent_id="agent-target",
            child_input={"prompt": "safe"},
            idempotency_key="key-a",
        )
    )

    assert result.parent_run_id == "parent-a"
    assert recorder.identity == _identity()
    assert recorder.request == DelegationRequest(
        parent_run_id="parent-a",
        source_agent_id="agent-source",
        target_agent_id="agent-target",
        child_input={"prompt": "safe"},
        idempotency_key="key-a",
        request_id="request-a",
    )


@pytest.mark.parametrize(
    "forged_field",
    ["tenant_id", "parent_run_id", "source_agent_id", "request_id", "trace_id"],
)
def test_business_payload_cannot_forge_trusted_context(forged_field: str) -> None:
    """验证业务输入不能携带任何由 runtime 持有的可信关联字段。"""

    payload: dict[str, object] = {
        "target_agent_id": "agent-target",
        "child_input": {},
        "idempotency_key": "key-a",
        forged_field: "forged",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentDelegateInput.model_validate(payload)


@pytest.mark.asyncio
async def test_bound_module_rejects_business_identity_override() -> None:
    """验证已经绑定的 module 不接受调用方额外传入的伪造身份。"""

    recorder = _RecordingService()
    trusted_identity = _identity()
    module = AgentDelegationModule(cast(DelegationService, recorder)).bind_execution(
        identity=trusted_identity,
        tenant_id="tenant-a",
        run_id="parent-a",
        agent_id="agent-source",
        request_id=None,
        trace_id="trace-a",
    )

    forged_identity = trusted_identity.model_copy(
        update={
            "user_id": "forged-admin",
            "session_id": "forged-session",
            "roles": ["admin"],
            "permissions": ["*"],
        }
    )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await cast(Any, module).delegate(
            AgentDelegateInput(
                target_agent_id="agent-target",
                child_input={},
                idempotency_key="key-a",
            ),
            identity=forged_identity,
        )
    assert recorder.request is None


class _DetailOrchestrator:
    """按是否存在 parent relation 返回最小 run detail 的路由桩。"""

    def __init__(self, *, parent_run_id: str | None) -> None:
        """固定该桩返回的 parent relation，用于验证 API detail 分支。"""

        self.parent_run_id = parent_run_id

    async def get_run_detail(self, run_id: str, **_: object) -> RunDetailResult:
        """返回不触及存储的稳定 detail，以隔离 route 的摘要拼装责任。"""

        return RunDetailResult(
            run_id=run_id,
            agent_id="agent-target" if self.parent_run_id else "agent-source",
            status=RunStatus.COMPLETED,
            terminal_event="run.completed",
            parent_run_id=self.parent_run_id,
        )


class _SummaryService:
    """提供持久化 parent 摘要的只读桩，不承担 child 查询或聚合计算。"""

    async def get_parent_summary(self, **_: object) -> DelegationSummary:
        """返回包含 child、用量和 trace 引用的最小 durable summary。"""

        return DelegationSummary(
            parent_run_id="parent-a",
            children=[
                DelegationChildSummary(
                    run_id="child-a",
                    agent_id="agent-target",
                    status="completed",
                    usage_evidence_refs=["usage-a"],
                    trace_refs=["trace-a"],
                )
            ],
            input_tokens=3,
            output_tokens=2,
            latency_ms=7,
            cost_usd=0.25,
            budget_status="within_budget",
            trace_refs=["trace-a"],
        )


@pytest.mark.asyncio
async def test_run_detail_exposes_parent_relation_and_durable_summary() -> None:
    """验证 parent detail 附带 durable summary，而 child 仅暴露其 parent 关联。"""

    parent = await get_run_with_orchestrator(
        "parent-a",
        orchestrator=cast(Any, _DetailOrchestrator(parent_run_id=None)),
        identity=_identity(),
        delegation_service=cast(DelegationService, _SummaryService()),
        request_id="request-a",
    )
    child = await get_run_with_orchestrator(
        "child-a",
        orchestrator=cast(Any, _DetailOrchestrator(parent_run_id="parent-a")),
        identity=_identity(),
        delegation_service=None,
        request_id="request-b",
    )

    assert parent.agent_id == "agent-source"
    assert parent.parent_run_id is None
    assert parent.delegation_summary is not None
    assert parent.delegation_summary.children[0].run_id == "child-a"
    assert child.agent_id == "agent-target"
    assert child.parent_run_id == "parent-a"
    assert child.delegation_summary is None
