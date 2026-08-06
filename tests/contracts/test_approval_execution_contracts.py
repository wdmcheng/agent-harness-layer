"""审批续跑、grant 绑定和 at-most-once 工具执行合同测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    InMemoryRunQueue,
    RunOrchestrator,
    RunResult,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate, run_migrations
from agent_harness.storage.access_repositories import ApprovalResolutionRepositoryConflict
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
)
from agent_harness.storage.models import RunEventCapacityModel
from agent_harness.tools import (
    ApprovedToolGrantError,
    BuiltinTool,
    ToolCallRequest,
    ToolRegistry,
    ToolRuntimeContext,
    hash_tool_arguments,
)


def sqlite_dsn(path: Path) -> str:
    """将每个临时 SQLite 文件转换为异步 storage DSN，避免测试共用状态。"""

    return f"sqlite+aiosqlite:///{path}"


class _ApprovedToolExecutor:
    """最小审批型执行器：首次等待授权，恢复后只能通过受控的 approved-tool seam 调用。"""

    def __init__(self, registry: ToolRegistry, arguments: dict[str, Any]) -> None:
        """保存受控工具注册表和固定参数，使 grant 的参数哈希绑定可由合同稳定验证。"""

        self.registry = registry
        self.arguments = arguments

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """声明需要审批的工具 continuation，不在等待阶段执行任何外部工具副作用。"""

        del context
        return AgentExecutionResult.waiting(
            AgentApprovalRequest(
                action="shell.execute",
                resource="tool:shell",
                reason="dangerous test action",
                arguments_ref="artifact://pending-arguments",
                arguments_hash=hash_tool_arguments(self.arguments),
                continuation={"tool_name": "shell.execute"},
            )
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """使用已签发 grant 调用工具，并将受控结果映射为编排器可持久化的终态。"""

        tool_result = await self.registry.call_approved(
            ToolCallRequest(
                tool_name="shell.execute",
                arguments=self.arguments,
                agent_id=request.agent_id,
                run_id=request.run_id,
            ),
            context=ToolRuntimeContext(
                actor=context.identity,
                agent_id=request.agent_id,
                run_id=request.run_id,
            ),
            grant=grant,
        )
        if tool_result.status != "completed":
            message = tool_result.error.message if tool_result.error else "tool failed"
            return AgentExecutionResult.failed(message)
        return AgentExecutionResult.completed({"tool_result": tool_result.result})


async def build_approval_flow(
    tmp_path: Path,
    *,
    handler: Any,
    recovery_lease_timeout_seconds: float = 300.0,
    queue: InMemoryRunQueue | None = None,
    storage_dsn: str | None = None,
) -> tuple[
    SQLAlchemyStorage,
    LocalJsonlEventSink,
    ApprovalService,
    RunOrchestrator,
    IdentityContext,
    ToolRegistry,
    RunResult,
]:
    """组装隔离的审批、工具、事件与可选队列环境，供下游合同聚焦一个恢复边界。"""

    dsn = storage_dsn or sqlite_dsn(tmp_path / "approval.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "approval-events.jsonl")
    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="shell.execute",
                action="shell.execute",
                resource="tool:shell",
                input_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                handler=handler,
            )
        ],
        policy=PolicyEngine(provider=YamlPolicyProvider()),
        audit=None,
        artifact_store=artifact_store,
        storage=storage,
    )
    executor = _ApprovedToolExecutor(registry, {"command": "echo safe"})
    identity = IdentityContext.local_default(session_id="approval-contract")
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        identity=identity,
        executor_resolver=lambda _agent_id: executor,
        queue=queue,
    )
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        recovery_lease_timeout_seconds=recovery_lease_timeout_seconds,
    )
    if queue is None:
        waiting = await orchestrator.start_run(agent_id="examples.dev", input={"prompt": "act"})
    else:
        submitted = await orchestrator.submit_run(
            agent_id="examples.dev",
            input={"prompt": "act"},
            identity=identity,
        )
        delivery = await queue.pickup(consumer_id="initial-service-worker")
        assert delivery is not None
        waiting = await orchestrator.execute_run(
            run_id=submitted.run_id,
            tenant_id=identity.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="initial-service-owner",
            workflow_id="initial-service-workflow",
        )
        await queue.ack(delivery.receipt)
    return storage, sink, service, orchestrator, identity, registry, waiting


__all__ = [
    "AgentApprovalRequest",
    "AgentExecutionContext",
    "AgentExecutionRequest",
    "AgentExecutionResult",
    "Any",
    "ApprovalGrant",
    "ApprovalResolutionRepositoryConflict",
    "ApprovalService",
    "ApprovalStateConflict",
    "ApprovedToolGrantError",
    "AuditService",
    "BuiltinTool",
    "EventBus",
    "EventCapacityExceeded",
    "FileArtifactStore",
    "IdentityContext",
    "InMemoryRunQueue",
    "LocalJsonlEventSink",
    "MAX_EVENT_SEQ",
    "Path",
    "PolicyEngine",
    "RunEventCapacityModel",
    "RunOrchestrator",
    "RunResult",
    "RunStatus",
    "SQLAlchemyStorage",
    "ToolCallRequest",
    "ToolInvocationCreate",
    "ToolRegistry",
    "ToolRuntimeContext",
    "YamlPolicyProvider",
    "_ApprovedToolExecutor",
    "asyncio",
    "build_approval_flow",
    "hash_tool_arguments",
    "pytest",
    "run_migrations",
    "sqlite_dsn",
    "update",
]
