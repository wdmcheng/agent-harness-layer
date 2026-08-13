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
from agent_harness.storage.model_tool_loop_marker import mark_model_tool_loop_evidence_seen
from agent_harness.storage.model_tool_loop_repositories import require_model_tool_loop_active
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
    RunExecutionContextRecord as RunExecutionContextRecord,
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
    loop_id: str | None = None
    turn_ordinal: int | None = None
    tool_call_id: str | None = None
    input_identity_digest: str | None = None
    output_digest: str | None = None


class ContextAssemblyRecord(ContextAssemblyCreate):
    """已持久化的 context assembly 摘要。"""

    id: str


class ContextAssemblyReplayConflict(RuntimeError):
    """相同loop-turn携带不同输入、输出或tool binding时的稳定失败。"""

    code = "context.assembly_replay_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


def _tenant_record(model: TenantModel) -> TenantRecord:
    """把租户 ORM 行投影为最小公开 DTO，不将会话绑定对象泄露到运行时。"""

    return TenantRecord(id=model.id, display_name=model.display_name)


# mapping helper 故意写得直白。它们不花哨，但 schema 名、JSON column 和公开
# DTO 字段最容易在这里漂移。
def _session_record(model: SessionModel) -> SessionRecord:
    """把 session ORM 行投影为 DTO，保持 JSON 元数据原样供身份边界复用。"""

    return SessionRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        agent_id=model.agent_id,
        metadata=model.metadata_json,
    )


def _checkpoint_record(model: CheckpointModel) -> CheckpointRecord:
    """把 checkpoint ORM 行投影为 DTO，使恢复层只依赖稳定字段而非 ORM 行状态。"""

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
    """把 context assembly ORM 行投影为可审计摘要，不让组装服务持有持久化对象。"""

    return ContextAssemblyRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        run_id=model.run_id,
        input_refs=model.input_refs_json,
        token_budget=model.token_budget,
        trust_summary=model.trust_summary_json,
        truncation_summary=model.truncation_summary_json,
        output_ref=model.output_ref,
        loop_id=model.loop_id,
        turn_ordinal=model.turn_ordinal,
        tool_call_id=model.tool_call_id,
        input_identity_digest=model.input_identity_digest,
        output_digest=model.output_digest,
    )


class TenantRepository:
    """租户表仓储，负责按需创建本地默认租户或返回已有租户。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定外层工作单元会话；创建后的提交由调用方和关联记录统一协调。"""

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
    """session 表仓储，封装 local identity 的 session 复用与创建。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定外层工作单元会话，不在仓储内独立提交 session 写入。"""

        self._session = session

    async def get(self, session_id: str) -> SessionRecord | None:
        """按稳定 session id 查询记录；不存在时返回 ``None`` 供调用方决定是否创建。"""

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
        """优先复用调用方提供的稳定 session id；缺失或未找到时创建新记录。"""

        if data.session_id is not None:
            # IdentityContext 会给 local/default run 提供稳定 session id。复用它可以
            # 让 CLI/API run 归组，同时不把 ORM session 概念暴露到 storage 外。
            model = await self._session.get(SessionModel, data.session_id)
            if model is not None:
                return _session_record(model)
        return await self.create(data)


class CheckpointRepository:
    """checkpoint 表仓储，提供最新恢复点和 resume token 查询。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定外层工作单元会话，使 checkpoint 与 run 状态可在同一事务内写入。"""

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
        """按 sequence 倒序读取指定 run 的最新耐久 checkpoint。"""

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
        """按不可预测的 resume token 查询恢复点；授权与 token 消费语义由上层控制。"""

        result = await self._session.scalars(
            select(CheckpointModel).where(CheckpointModel.resume_token == resume_token)
        )
        model = result.first()
        return None if model is None else _checkpoint_record(model)


class ContextAssemblyRepository:
    """ContextAssembler 专用仓储，避免业务代码直接写 ORM model。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定外层工作单元会话，确保组装摘要和关联 run 能原子提交。"""

        self._session = session

    async def create(self, data: ContextAssemblyCreate) -> ContextAssemblyRecord:
        """保存一次 ContextAssembler 的输入 refs、trust 和截断摘要。"""

        if data.loop_id is not None:
            await require_model_tool_loop_active(
                self._session,
                tenant_id=data.tenant_id,
                loop_id=data.loop_id,
            )
            existing = await self.get_by_loop_turn(
                tenant_id=data.tenant_id,
                loop_id=data.loop_id,
                turn_ordinal=data.turn_ordinal,
            )
            if existing is not None:
                if not _context_assembly_matches(existing, data):
                    raise ContextAssemblyReplayConflict
                return existing
            await mark_model_tool_loop_evidence_seen(self._session)
        model = ContextAssemblyModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            run_id=data.run_id,
            input_refs_json=data.input_refs,
            token_budget=data.token_budget,
            trust_summary_json=data.trust_summary,
            truncation_summary_json=data.truncation_summary,
            output_ref=data.output_ref,
            loop_id=data.loop_id,
            turn_ordinal=data.turn_ordinal,
            tool_call_id=data.tool_call_id,
            input_identity_digest=data.input_identity_digest,
            output_digest=data.output_digest,
        )
        self._session.add(model)
        await self._session.flush()
        return _context_assembly_record(model)

    async def get(self, assembly_id: str) -> ContextAssemblyRecord | None:
        """按主键读取组装摘要；不存在时返回 ``None``，不把 ORM 行暴露给上层。"""

        model = await self._session.get(ContextAssemblyModel, assembly_id)
        return None if model is None else _context_assembly_record(model)

    async def get_by_loop_turn(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        turn_ordinal: int | None,
        for_update: bool = False,
    ) -> ContextAssemblyRecord | None:
        """读取新循环唯一assembly；legacy NULL identity永不匹配。"""

        statement = select(ContextAssemblyModel).where(
            ContextAssemblyModel.tenant_id == tenant_id,
            ContextAssemblyModel.loop_id == loop_id,
            ContextAssemblyModel.turn_ordinal == turn_ordinal,
        )
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
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
        if model.output_ref != "pending://context-assembly-output":
            if model.output_ref != output_ref:
                raise ContextAssemblyReplayConflict
            return _context_assembly_record(model)
        model.output_ref = output_ref
        await self._session.flush()
        return _context_assembly_record(model)


def _context_assembly_matches(
    existing: ContextAssemblyRecord,
    data: ContextAssemblyCreate,
) -> bool:
    """重放时比较全部输入/输出摘要，但允许placeholder已提升为真实artifact ref。"""

    return (
        existing.run_id == data.run_id
        and existing.input_refs == data.input_refs
        and existing.token_budget == data.token_budget
        and existing.trust_summary == data.trust_summary
        and existing.truncation_summary == data.truncation_summary
        and existing.loop_id == data.loop_id
        and existing.turn_ordinal == data.turn_ordinal
        and existing.tool_call_id == data.tool_call_id
        and existing.input_identity_digest == data.input_identity_digest
        and existing.output_digest == data.output_digest
    )
