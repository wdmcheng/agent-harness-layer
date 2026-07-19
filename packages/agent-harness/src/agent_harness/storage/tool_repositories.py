"""工具执行相关 repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import ToolInvocationModel, WorkspaceModel
from agent_harness.storage.run_trace_gate import project_canonical_run_trace


class WorkspaceCreate(HarnessDTO):
    """创建 workspace 记录的公开输入。"""

    tenant_id: str
    agent_id: str
    run_id: str | None = None
    root_path: str
    policy_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceRecord(WorkspaceCreate):
    """已持久化 workspace 摘要。"""

    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ToolInvocationCreate(HarnessDTO):
    """创建 tool_invocations 记录的公开输入。"""

    tenant_id: str
    agent_id: str
    run_id: str | None = None
    tool_name: str
    args_ref: str
    result_ref: str | None = None
    approval_id: str | None = None
    arguments_hash: str | None = None
    execution_state: str | None = None
    status: str
    duration_ms: int | None = None
    trace_id: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationRecord(ToolInvocationCreate):
    """已持久化工具调用摘要。"""

    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkspaceRepository:
    """workspace 表 repository，调用方不直接接触 ORM model。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前 UoW session；调用方负责事务提交或回滚。"""

        self._session = session

    async def create(self, data: WorkspaceCreate) -> WorkspaceRecord:
        """创建一个带策略引用的 workspace 记录，不解释或访问宿主文件路径。"""

        model = WorkspaceModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            root_path=data.root_path,
            policy_ref=data.policy_ref,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _workspace_record(model)

    async def get(self, workspace_id: str) -> WorkspaceRecord | None:
        """按主键读取 workspace 摘要，缺失时返回 ``None`` 供服务层映射。"""

        model = await self._session.get(WorkspaceModel, workspace_id)
        return None if model is None else _workspace_record(model)


class ToolInvocationRepository:
    """tool_invocations 表 repository，保存参数/result artifact 引用。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定工具调用持久化所使用的当前异步 session。"""

        self._session = session

    async def create(self, data: ToolInvocationCreate) -> ToolInvocationRecord:
        """持久化工具调用元数据，并在关联运行时投影可信 canonical trace。"""

        trace_id = data.trace_id
        if data.run_id is not None:
            # 调用方传入的 trace 只能作为一致性校验，不能覆盖已持久化的运行归属。
            trace_id = await project_canonical_run_trace(
                self._session,
                tenant_id=data.tenant_id,
                run_id=data.run_id,
                trace_id=data.trace_id,
            )
        model = ToolInvocationModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            tool_name=data.tool_name,
            args_ref=data.args_ref,
            result_ref=data.result_ref,
            approval_id=data.approval_id,
            arguments_hash=data.arguments_hash,
            execution_state=data.execution_state,
            status=data.status,
            duration_ms=data.duration_ms,
            trace_id=trace_id,
            request_id=data.request_id,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _tool_invocation_record(model)

    async def get(self, invocation_id: str) -> ToolInvocationRecord | None:
        """按主键读取工具调用记录，不改变 approval 或执行状态。"""

        model = await self._session.get(ToolInvocationModel, invocation_id)
        return None if model is None else _tool_invocation_record(model)

    async def get_by_approval_id(self, approval_id: str) -> ToolInvocationRecord | None:
        """读取审批对应的唯一工具 claim，供续跑和审计路径复用。"""

        result = await self._session.scalars(
            select(ToolInvocationModel).where(ToolInvocationModel.approval_id == approval_id)
        )
        model = result.first()
        return None if model is None else _tool_invocation_record(model)

    async def finish_approved_claim(
        self,
        *,
        approval_id: str,
        result_ref: str,
        execution_state: str,
        status: str,
    ) -> ToolInvocationRecord:
        """用一个确定性 result artifact 封存唯一 approval claim。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.approval_id == approval_id,
                    ToolInvocationModel.execution_state == "executing",
                    ToolInvocationModel.result_ref.is_(None),
                )
                .values(
                    result_ref=result_ref,
                    execution_state=execution_state,
                    status=status,
                )
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"approved tool claim cannot be finalized: {approval_id}")
        record = await self.get_by_approval_id(approval_id)
        if record is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approved tool claim not found: {approval_id}")
        return record


def _workspace_record(model: WorkspaceModel) -> WorkspaceRecord:
    """将 workspace ORM 模型映射为公共记录，隔离 SQLAlchemy 对象生命周期。"""

    return WorkspaceRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        run_id=model.run_id,
        root_path=model.root_path,
        policy_ref=model.policy_ref,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _tool_invocation_record(model: ToolInvocationModel) -> ToolInvocationRecord:
    """将工具调用 ORM 模型映射为含 artifact 引用的领域摘要。"""

    return ToolInvocationRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        run_id=model.run_id,
        tool_name=model.tool_name,
        args_ref=model.args_ref,
        result_ref=model.result_ref,
        approval_id=model.approval_id,
        arguments_hash=model.arguments_hash,
        execution_state=model.execution_state,
        status=model.status,
        duration_ms=model.duration_ms,
        trace_id=model.trace_id,
        request_id=model.request_id,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
