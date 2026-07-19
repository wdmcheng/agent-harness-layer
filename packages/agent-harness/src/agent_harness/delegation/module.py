"""内置 `agent.delegate` module：业务 payload 不能覆盖可信 run 上下文。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.delegation.models import DelegationRequest
from agent_harness.delegation.service import DelegationExecutionResult, DelegationService
from agent_harness.identity import IdentityContext


class AgentDelegateInput(HarnessDTO):
    """业务 agent 只提供目标、child input 与显式幂等 key。"""

    target_agent_id: str = Field(min_length=1)
    child_input: dict[str, Any]
    idempotency_key: str = Field(min_length=1)


class AgentDelegationModule:
    """实现 RunBoundExecutionService，绑定值只来自 runtime composition。"""

    name = "agent.delegate"

    def __init__(self, service: DelegationService) -> None:
        """保存 application service；绑定后的业务调用只能经受控 facade 到达它。"""

        self._service = service

    async def recover_pending_for_parent(self, *, parent_run_id: str) -> int:
        """供 runtime 重放 WAITING parent，不向业务 executor 暴露恢复入口。"""

        return await self._service.recover_pending_for_parent(parent_run_id=parent_run_id)

    def bind_execution(
        self,
        *,
        identity: IdentityContext,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> BoundAgentDelegationModule:
        """将 runtime 已验证的 parent 身份、trace 与请求上下文封装为一次性调用面。

        业务 payload 不接收这些可信字段，避免 executor 伪造跨租户 parent、source
        agent 或 trace；identity 使用深拷贝，防止调用方在绑定后修改权限集合。
        """

        if identity.tenant_id != tenant_id:
            raise ValueError("delegation execution identity does not match bound tenant")
        return BoundAgentDelegationModule(
            service=self._service,
            identity=identity.model_copy(deep=True),
            tenant_id=tenant_id,
            parent_run_id=run_id,
            source_agent_id=agent_id,
            request_id=request_id,
            trace_id=trace_id,
        )


class BoundAgentDelegationModule:
    """一次 executor 调用可见的封闭 delegation seam。"""

    def __init__(
        self,
        *,
        service: DelegationService,
        identity: IdentityContext,
        tenant_id: str,
        parent_run_id: str,
        source_agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> None:
        """保存 immutable runtime 绑定值；仅 target、child input 与幂等键来自业务侧。"""

        self._service = service
        self._identity = identity
        self._tenant_id = tenant_id
        self._parent_run_id = parent_run_id
        self._source_agent_id = source_agent_id
        self._request_id = request_id
        self._trace_id = trace_id

    async def delegate(
        self,
        request: AgentDelegateInput,
    ) -> DelegationExecutionResult:
        """将受限业务输入与可信绑定上下文合成为 delegation 请求并交给 service。

        trace 缺失即失败，不能由业务输入补写；这样可确保 child relation、事件和预算
        identity 共享 parent 的 canonical trace 范围。
        """

        if not self._trace_id:
            raise ValueError("delegation execution requires a canonical trace")
        return await self._service.delegate(
            DelegationRequest(
                parent_run_id=self._parent_run_id,
                source_agent_id=self._source_agent_id,
                target_agent_id=request.target_agent_id,
                child_input=request.child_input,
                idempotency_key=request.idempotency_key,
                request_id=self._request_id,
            ),
            identity=self._identity.model_copy(deep=True),
        )


__all__ = [
    "AgentDelegateInput",
    "AgentDelegationModule",
    "BoundAgentDelegationModule",
]
