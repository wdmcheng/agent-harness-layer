"""最小可运行 Agent 示例，供脚手架、运行时回滚与本地冒烟测试复用。"""

from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class BasicAgentExecutor:
    """以真实执行器协议承载稳定的本地测试响应。

    该示例不访问模型、工具或外部服务，目的是让新建服务先验证执行器
    注册、调用和恢复链路。固定输出是测试夹具契约的一部分，不应扩展为
    业务逻辑或作为生产 Agent 的实现基础。
    """

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """返回用于健康检查的确定性完成结果。

        请求和上下文在这里刻意不参与计算：这样 smoke 测试可以隔离运行时
        注入差异，只验证 executor protocol 是否被正确接入。
        """
        del request, context
        return AgentExecutionResult.completed({"result": "fake-ok"})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """模拟恢复入口，确认运行时可传递审批授权对象。

        最小示例没有待审批动作，因此不解释授权内容；仍保留该方法以满足
        所有 Agent 共享的恢复协议，并覆盖调用方不会遗漏 continuation 分支。
        """
        del request, context, grant
        return AgentExecutionResult.completed({"resumed": True})


executor = BasicAgentExecutor()
