"""工具执行相关 repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import ToolInvocationModel, WorkspaceModel


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
        self._session = session

    async def create(self, data: WorkspaceCreate) -> WorkspaceRecord:
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
        model = await self._session.get(WorkspaceModel, workspace_id)
        return None if model is None else _workspace_record(model)


class ToolInvocationRepository:
    """tool_invocations 表 repository，保存参数/result artifact 引用。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ToolInvocationCreate) -> ToolInvocationRecord:
        model = ToolInvocationModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            tool_name=data.tool_name,
            args_ref=data.args_ref,
            result_ref=data.result_ref,
            status=data.status,
            duration_ms=data.duration_ms,
            trace_id=data.trace_id,
            request_id=data.request_id,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _tool_invocation_record(model)

    async def get(self, invocation_id: str) -> ToolInvocationRecord | None:
        model = await self._session.get(ToolInvocationModel, invocation_id)
        return None if model is None else _tool_invocation_record(model)


def _workspace_record(model: WorkspaceModel) -> WorkspaceRecord:
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
    return ToolInvocationRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        run_id=model.run_id,
        tool_name=model.tool_name,
        args_ref=model.args_ref,
        result_ref=model.result_ref,
        status=model.status,
        duration_ms=model.duration_ms,
        trace_id=model.trace_id,
        request_id=model.request_id,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
