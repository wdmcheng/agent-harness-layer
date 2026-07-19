"""Runtime 合同文件共享的确定性 executor 与独立 SQLite DSN。"""

from pathlib import Path

from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class FakeContractExecutor:
    """显式 fake；runtime 本身不得在 provider 缺失时静默 fallback。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """返回固定完成结果，隔离 runtime 编排测试与真实模型或工具副作用。"""

        del request, context
        return AgentExecutionResult.completed({"result": "fake-ok"})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """返回固定恢复结果，验证审批恢复协议而不执行外部动作。"""

        del request, context, grant
        return AgentExecutionResult.completed({"resumed": True})


def sqlite_dsn(path: Path) -> str:
    """为每个 runtime 合同返回独立 SQLite DSN。"""

    return f"sqlite+aiosqlite:///{path}"
