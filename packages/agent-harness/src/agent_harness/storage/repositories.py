"""Core repository 实现与历史导入路径兼容门面。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.access_repositories import (
    ApiKeyCreate as ApiKeyCreate,
)
from agent_harness.storage.access_repositories import (
    ApiKeyRecord as ApiKeyRecord,
)
from agent_harness.storage.access_repositories import (
    ApiKeyRepository as ApiKeyRepository,
)
from agent_harness.storage.access_repositories import (
    ApprovalCreate as ApprovalCreate,
)
from agent_harness.storage.access_repositories import (
    ApprovalRecord as ApprovalRecord,
)
from agent_harness.storage.access_repositories import (
    ApprovalRepository as ApprovalRepository,
)
from agent_harness.storage.access_repositories import (
    PolicyRuleCreate as PolicyRuleCreate,
)
from agent_harness.storage.access_repositories import (
    PolicyRuleRecord as PolicyRuleRecord,
)
from agent_harness.storage.access_repositories import (
    PolicyRuleRepository as PolicyRuleRepository,
)
from agent_harness.storage.audit_repositories import (
    AuditLogCreate as AuditLogCreate,
)
from agent_harness.storage.audit_repositories import (
    AuditLogRecord as AuditLogRecord,
)
from agent_harness.storage.audit_repositories import (
    AuditLogRepository as AuditLogRepository,
)
from agent_harness.storage.embedding_cache_repositories import (
    EmbeddingCacheCreate as EmbeddingCacheCreate,
)
from agent_harness.storage.embedding_cache_repositories import (
    EmbeddingCacheRecord as EmbeddingCacheRecord,
)
from agent_harness.storage.embedding_cache_repositories import (
    EmbeddingCacheRepository as EmbeddingCacheRepository,
)
from agent_harness.storage.models import (
    CheckpointModel,
    ContextAssemblyModel,
    SessionModel,
    TenantModel,
)
from agent_harness.storage.run_repositories import (
    RunCreate as RunCreate,
)
from agent_harness.storage.run_repositories import (
    RunExecutionRecord as RunExecutionRecord,
)
from agent_harness.storage.run_repositories import (
    RunRecord as RunRecord,
)
from agent_harness.storage.run_repositories import (
    RunRepository as RunRepository,
)
from agent_harness.storage.run_repositories import (
    RunTraceRepositoryConflict as RunTraceRepositoryConflict,
)


# 以下 DTO 是 runtime/API/tests 能看到的公开数据形状。SQLAlchemy model 实例
# 留在 storage 包内，repository 行为才能用同一套断言覆盖 SQLite 和 PostgreSQL。
class TenantRecord(HarnessDTO):
    """repository 对外返回的租户记录。"""

    id: str
    display_name: str


class SessionCreate(HarnessDTO):
    """创建或复用 session 时的输入 DTO。"""

    session_id: str | None = None
    tenant_id: str
    user_id: str
    agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(SessionCreate):
    """已持久化的 session 记录。"""

    id: str


class CheckpointCreate(HarnessDTO):
    """创建 checkpoint/resume token 时的输入 DTO。"""

    tenant_id: str
    run_id: str
    sequence: int
    resume_token: str
    state: dict[str, Any] = Field(default_factory=dict)


class CheckpointRecord(CheckpointCreate):
    """已持久化的 checkpoint 记录。"""

    id: str
    created_at: datetime | None = None


class ContextAssemblyCreate(HarnessDTO):
    """写入 context_assemblies 时的审计摘要。"""

    tenant_id: str
    run_id: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    token_budget: int
    trust_summary: dict[str, Any] = Field(default_factory=dict)
    truncation_summary: dict[str, Any] = Field(default_factory=dict)
    output_ref: str


class ContextAssemblyRecord(ContextAssemblyCreate):
    """已持久化的 context assembly 摘要。"""

    id: str


def _tenant_record(model: TenantModel) -> TenantRecord:
    return TenantRecord(id=model.id, display_name=model.display_name)


# mapping helper 故意写得直白。它们不花哨，但 schema 名、JSON column 和公开
# DTO 字段最容易在这里漂移。
def _session_record(model: SessionModel) -> SessionRecord:
    return SessionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        agent_id=model.agent_id,
        metadata=model.metadata_json,
    )


def _checkpoint_record(model: CheckpointModel) -> CheckpointRecord:
    return CheckpointRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        run_id=model.run_id,
        sequence=model.sequence,
        resume_token=model.resume_token,
        state=model.state_json,
        created_at=model.created_at,
    )


def _context_assembly_record(model: ContextAssemblyModel) -> ContextAssemblyRecord:
    return ContextAssemblyRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        run_id=model.run_id,
        input_refs=model.input_refs_json,
        token_budget=model.token_budget,
        trust_summary=model.trust_summary_json,
        truncation_summary=model.truncation_summary_json,
        output_ref=model.output_ref,
    )


class TenantRepository:
    """租户表 repository，负责按需创建 default tenant。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure(self, tenant_id: str, display_name: str | None = None) -> TenantRecord:
        """确保 tenant 存在，local profile 可用 `default` 作为稳定归属。"""

        model = await self._session.get(TenantModel, tenant_id)
        if model is None:
            model = TenantModel(id=tenant_id, display_name=display_name or tenant_id)
            self._session.add(model)
            await self._session.flush()
        return _tenant_record(model)


class SessionRepository:
    """session 表 repository，封装 local identity 的 session 复用。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, session_id: str) -> SessionRecord | None:
        model = await self._session.get(SessionModel, session_id)
        return None if model is None else _session_record(model)

    async def create(self, data: SessionCreate) -> SessionRecord:
        """创建 session 记录；调用方决定是否传入稳定 session_id。"""

        model = SessionModel(
            id=data.session_id or str(uuid4()),
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            agent_id=data.agent_id,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _session_record(model)

    async def ensure(self, data: SessionCreate) -> SessionRecord:
        if data.session_id is not None:
            # IdentityContext 会给 local/default run 提供稳定 session id。复用它可以
            # 让 CLI/API run 归组，同时不把 ORM session 概念暴露到 storage 外。
            model = await self._session.get(SessionModel, data.session_id)
            if model is not None:
                return _session_record(model)
        return await self.create(data)


class CheckpointRepository:
    """checkpoint 表 repository，提供 latest 和 resume token 查询。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: CheckpointCreate) -> CheckpointRecord:
        """持久化 checkpoint state 和 resume token。"""

        model = CheckpointModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            run_id=data.run_id,
            sequence=data.sequence,
            resume_token=data.resume_token,
            state_json=data.state,
        )
        self._session.add(model)
        await self._session.flush()
        return _checkpoint_record(model)

    async def get_latest(self, run_id: str) -> CheckpointRecord | None:
        # resume 以最高 checkpoint sequence 作为持久化真相源。后续 worker 开始
        # 给长流程打 checkpoint 后，这个排序语义会变成关键边界。
        result = await self._session.scalars(
            select(CheckpointModel)
            .where(CheckpointModel.run_id == run_id)
            .order_by(CheckpointModel.sequence.desc())
            .limit(1)
        )
        model = result.first()
        return None if model is None else _checkpoint_record(model)

    async def get_by_resume_token(self, resume_token: str) -> CheckpointRecord | None:
        result = await self._session.scalars(
            select(CheckpointModel).where(CheckpointModel.resume_token == resume_token)
        )
        model = result.first()
        return None if model is None else _checkpoint_record(model)


class ContextAssemblyRepository:
    """ContextAssembler 专用 repository，避免业务代码直接写 ORM model。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ContextAssemblyCreate) -> ContextAssemblyRecord:
        """保存一次 ContextAssembler 的输入 refs、trust 和截断摘要。"""

        model = ContextAssemblyModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            run_id=data.run_id,
            input_refs_json=data.input_refs,
            token_budget=data.token_budget,
            trust_summary_json=data.trust_summary,
            truncation_summary_json=data.truncation_summary,
            output_ref=data.output_ref,
        )
        self._session.add(model)
        await self._session.flush()
        return _context_assembly_record(model)

    async def get(self, assembly_id: str) -> ContextAssemblyRecord | None:
        model = await self._session.get(ContextAssemblyModel, assembly_id)
        return None if model is None else _context_assembly_record(model)

    async def update_output_ref(
        self,
        assembly_id: str,
        *,
        output_ref: str,
    ) -> ContextAssemblyRecord:
        """在同一 UoW 内把组装完成后的真实 artifact ref 固定到记录。"""

        model = await self._session.get(ContextAssemblyModel, assembly_id)
        if model is None:
            raise LookupError(f"context assembly not found: {assembly_id}")
        model.output_ref = output_ref
        await self._session.flush()
        return _context_assembly_record(model)
