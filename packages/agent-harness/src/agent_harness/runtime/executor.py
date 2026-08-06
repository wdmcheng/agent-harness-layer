"""Agent execution 的 provider-neutral contract。

runtime 负责生命周期持久化，应用 agent 负责业务编排。request/context/result
保持可序列化；进程内服务通过 context 的 private mapping 注入，不进入 checkpoint、
API、trace 或 eval payload。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, PrivateAttr, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext
from agent_harness.runtime.checkpoints import ResumeToken
from agent_harness.runtime.state import RunStatus


class AgentExecutionRequest(HarnessDTO):
    """一次持久化 run 交给 agent executor 的输入。"""

    agent_id: str
    run_id: str
    input: dict[str, Any] = Field(default_factory=dict)


class AgentExecutionContext(HarnessDTO):
    """executor 继承的可序列化身份与关联上下文。"""

    identity: IdentityContext
    request_id: str | None = None
    trace_id: str | None = None
    _services: Mapping[str, object] = PrivateAttr(default_factory=dict)

    def bind_services(self, services: Mapping[str, object]) -> AgentExecutionContext:
        """绑定本进程公共服务；private 属性不会进入任何 DTO payload。"""

        self._services = MappingProxyType(dict(services))
        return self

    def require_service(self, name: str) -> object:
        """读取 composition 已注入的服务，缺失时以稳定错误阻止降级旁路。"""

        try:
            return self._services[name]
        except KeyError as exc:
            raise AgentExecutionServiceUnavailable(
                f"agent execution service is not configured: {name}"
            ) from exc


@runtime_checkable
class RunBoundExecutionService(Protocol):
    """由 composition 在业务可见前绑定可信 run 关联的服务。"""

    def bind_execution(
        self,
        *,
        identity: IdentityContext,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> object:
        """将服务封闭到已验证的运行、租户、请求和 trace，返回业务可见 facade。"""
        ...


def build_execution_context(
    *,
    identity: IdentityContext,
    services: Mapping[str, object],
    agent_id: str,
    run_id: str,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> AgentExecutionContext:
    """构造 context，并把支持绑定的服务封闭到当前可信 run。"""

    if not trace_id:
        raise ValueError("agent execution context requires canonical trace_id")
    bound_services = {
        name: (
            service.bind_execution(
                identity=identity,
                tenant_id=identity.tenant_id,
                run_id=run_id,
                agent_id=agent_id,
                request_id=request_id,
                trace_id=trace_id,
            )
            if isinstance(service, RunBoundExecutionService)
            else service
        )
        for name, service in services.items()
    }

    return AgentExecutionContext(
        identity=identity,
        request_id=request_id,
        trace_id=trace_id,
    ).bind_services(bound_services)


class AgentApprovalRequest(HarnessDTO):
    """创建公开 approval record 所需的脱敏 continuation 数据。"""

    action: str
    resource: str
    reason: str
    arguments_ref: str
    arguments_hash: str
    continuation: dict[str, Any] = Field(default_factory=dict)


class ApprovalGrant(HarnessDTO):
    """把一个 approval lease 绑定到唯一待执行动作的授权能力。"""

    approval_id: str
    lease_id: str
    tenant_id: str
    identity_id: str
    session_id: str
    agent_id: str
    run_id: str
    action: str
    resource: str
    arguments_hash: str


class RunResult(HarnessDTO):
    """runtime seam 返回给 API、CLI 和 approval 的 run 摘要。"""

    run_id: str
    status: RunStatus
    terminal_event: str | None = None
    resume_token: ResumeToken | None = None


class RunDetailResult(HarnessDTO):
    """RUN-002 适配层读取的 provider-neutral durable run detail。"""

    run_id: str
    agent_id: str
    status: RunStatus
    terminal_event: str | None = None
    parent_run_id: str | None = None


class AgentExecutionResult(HarnessDTO):
    """供 ``RunOrchestrator`` 消费的 typed executor 结果。"""

    status: Literal["completed", "waiting", "failed"]
    output: dict[str, Any] | None = None
    error: str | None = None
    approval: AgentApprovalRequest | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> AgentExecutionResult:
        """验证 completed、waiting、failed 三种结果的互斥字段组合。

        执行器结果会驱动终态持久化或审批创建；在 DTO 边界拒绝混合输出、错误和
        授权对象，避免编排器面对含糊状态时猜测应该发布哪一种事件。
        """
        if self.status == "completed" and self.output is None:
            raise ValueError("completed execution requires output")
        if self.status == "waiting" and self.approval is None:
            raise ValueError("waiting execution requires approval")
        if self.status == "failed" and not self.error:
            raise ValueError("failed execution requires error")
        if self.status != "waiting" and self.approval is not None:
            raise ValueError("approval is only valid for waiting execution")
        if self.status != "completed" and self.output is not None:
            raise ValueError("output is only valid for completed execution")
        if self.status != "failed" and self.error is not None:
            raise ValueError("error is only valid for failed execution")
        return self

    @classmethod
    def completed(cls, output: dict[str, Any]) -> AgentExecutionResult:
        """构造带业务输出的完成结果，交由编排器持久化终态和公开事件。"""
        return cls(status="completed", output=output)

    @classmethod
    def waiting(cls, approval: AgentApprovalRequest) -> AgentExecutionResult:
        """构造等待人工授权的结果；审批参数已脱敏并通过 artifact 引用保存。"""
        return cls(status="waiting", approval=approval)

    @classmethod
    def failed(cls, error: str) -> AgentExecutionResult:
        """构造稳定失败结果，避免 executor 直接写入运行状态绕过生命周期栅栏。"""
        return cls(status="failed", error=error)


@runtime_checkable
class AgentExecutor(Protocol):
    """从受控 agent package reference 解析的业务 executor。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """执行首次请求，返回完成、等待审批或失败三种显式结果之一。"""
        ...

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """在编排器验证 ApprovalGrant 后继续等待中的动作，不重新解释授权参数。"""
        ...


AgentExecutorResolver = Callable[[str], AgentExecutor]


class AgentExecutionUncertain(RuntimeError):
    """副作用可能已开始，但没有持久化确定性结果。"""


class AgentExecutionLeaseLost(RuntimeError):
    """当前 owner 的 approval lease 已被过期接管，必须停止且不得改写 run。"""


class AgentExecutionServiceUnavailable(RuntimeError):
    """composition 未注入 executor 声明需要的公共服务。"""
