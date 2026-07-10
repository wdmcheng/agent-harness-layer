"""Repository DTO 与 SQLAlchemy repository 实现。"""

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
from agent_harness.storage.models import (
    AgentRunModel,
    CheckpointModel,
    ContextAssemblyModel,
    EmbeddingCacheModel,
    SessionModel,
    TenantModel,
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


class RunCreate(HarnessDTO):
    """创建 run 时写入 repository 的稳定输入。"""

    tenant_id: str
    session_id: str
    agent_id: str
    idempotency_key: str | None = None
    parent_run_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class RunRecord(RunCreate):
    """repository 对 runtime 返回的 run 摘要。"""

    id: str
    status: str
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


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


class EmbeddingCacheCreate(HarnessDTO):
    """写入 embedding_cache 的 provider/model/input_hash 记录。"""

    tenant_id: str
    provider: str
    model: str
    input_hash: str
    vector_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingCacheRecord(EmbeddingCacheCreate):
    """已持久化的 embedding cache 记录。"""

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


def _run_record(model: AgentRunModel) -> RunRecord:
    return RunRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        agent_id=model.agent_id,
        idempotency_key=model.idempotency_key,
        parent_run_id=model.parent_run_id,
        input=model.input_json,
        status=model.status,
        output=model.output_json,
        error=model.error_json,
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


def _embedding_cache_record(model: EmbeddingCacheModel) -> EmbeddingCacheRecord:
    return EmbeddingCacheRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        provider=model.provider,
        model=model.model,
        input_hash=model.input_hash,
        vector_ref=model.vector_ref,
        metadata=model.metadata_json,
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


class RunRepository:
    """run 表 repository，集中处理幂等键和状态更新。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: RunCreate) -> RunRecord:
        """创建 run 记录，并在幂等键命中时返回既有记录。"""

        if data.idempotency_key is not None:
            # 创建前先处理 idempotency，重复 API/CLI 提交会收敛到同一条持久化
            # AgentRun 记录。
            existing = await self.get_by_idempotency_key(
                tenant_id=data.tenant_id,
                session_id=data.session_id,
                agent_id=data.agent_id,
                idempotency_key=data.idempotency_key,
            )
            if existing is not None:
                return existing

        model = AgentRunModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            session_id=data.session_id,
            agent_id=data.agent_id,
            idempotency_key=data.idempotency_key,
            parent_run_id=data.parent_run_id,
            input_json=data.input,
            status="created",
        )
        self._session.add(model)
        await self._session.flush()
        return _run_record(model)

    async def get(self, run_id: str) -> RunRecord | None:
        model = await self._session.get(AgentRunModel, run_id)
        return None if model is None else _run_record(model)

    async def get_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        session_id: str,
        agent_id: str,
        idempotency_key: str,
    ) -> RunRecord | None:
        """按 tenant/session/agent/idempotency_key 查找重复提交。"""

        result = await self._session.scalars(
            select(AgentRunModel).where(
                AgentRunModel.tenant_id == tenant_id,
                AgentRunModel.session_id == session_id,
                AgentRunModel.agent_id == agent_id,
                AgentRunModel.idempotency_key == idempotency_key,
            )
        )
        model = result.first()
        return None if model is None else _run_record(model)

    async def set_status(
        self,
        run_id: str,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> RunRecord:
        model = await self._session.get(AgentRunModel, run_id)
        if model is None:
            raise LookupError(f"run not found: {run_id}")
        # 状态更新是唯一写 terminal output/error 的 repository 路径。集中在这里，
        # 防止 runtime adapter 直接改 JSON column。
        model.status = status
        model.output_json = output
        model.error_json = error
        await self._session.flush()
        return _run_record(model)


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


class EmbeddingCacheRepository:
    """EmbeddingProvider 专用 cache repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        *,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        result = await self._session.scalars(
            select(EmbeddingCacheModel).where(
                EmbeddingCacheModel.provider == provider,
                EmbeddingCacheModel.model == model,
                EmbeddingCacheModel.input_hash == input_hash,
            )
        )
        row = result.first()
        return None if row is None else _embedding_cache_record(row)

    async def put(self, data: EmbeddingCacheCreate) -> EmbeddingCacheRecord:
        """写入 embedding cache；已存在时返回既有 vector_ref。"""

        existing = await self.get(
            provider=data.provider,
            model=data.model,
            input_hash=data.input_hash,
        )
        if existing is not None:
            # 并发前先做幂等保护；唯一约束仍是跨进程写入的最终防线。
            return existing
        model = EmbeddingCacheModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            provider=data.provider,
            model=data.model,
            input_hash=data.input_hash,
            vector_ref=data.vector_ref,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _embedding_cache_record(model)
