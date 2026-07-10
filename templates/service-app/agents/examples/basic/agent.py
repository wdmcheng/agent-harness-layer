"""Basic 模板 agent 的显式 local smoke executor。"""

from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class BasicAgentExecutor:
    """把历史 smoke 输出放在真实 executor protocol 后面。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del request, context
        return AgentExecutionResult.completed({"result": "fake-ok"})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del request, context, grant
        return AgentExecutionResult.completed({"resumed": True})


executor = BasicAgentExecutor()
