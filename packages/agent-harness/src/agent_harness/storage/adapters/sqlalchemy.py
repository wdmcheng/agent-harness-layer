"""SQLAlchemy async storage adapter 与 Unit of Work。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_harness.storage.eval_repositories import (
    EvalCaseRepository,
    EvalRunRepository,
    EvalScoreRepository,
)
from agent_harness.storage.repositories import (
    ApiKeyRepository,
    ApprovalRepository,
    AuditLogRepository,
    CheckpointRepository,
    ContextAssemblyRepository,
    EmbeddingCacheRepository,
    PolicyRuleRepository,
    RunRepository,
    SessionRepository,
    TenantRepository,
)
from agent_harness.storage.retrieval_repositories import (
    RetrievalChunkRepository,
    RetrievalDocumentRepository,
)
from agent_harness.storage.settings import normalize_async_dsn
from agent_harness.storage.tool_repositories import (
    ToolInvocationRepository,
    WorkspaceRepository,
)


class SQLAlchemyUnitOfWork:
    """显式 commit 的事务边界；未 commit 离开时自动 rollback。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._committed = False
        self.session: AsyncSession

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        self.session = self._session_factory()
        # Repository 都绑定在同一个 AsyncSession 上，调用方只能通过 UoW 访问。
        # 这保证一次 runtime 操作里的 tenant/session/run/checkpoint 修改同生共死。
        self.tenants = TenantRepository(self.session)
        self.sessions = SessionRepository(self.session)
        self.runs = RunRepository(self.session)
        self.checkpoints = CheckpointRepository(self.session)
        self.context_assemblies = ContextAssemblyRepository(self.session)
        self.embedding_cache = EmbeddingCacheRepository(self.session)
        self.retrieval_documents = RetrievalDocumentRepository(self.session)
        self.retrieval_chunks = RetrievalChunkRepository(self.session)
        self.api_keys = ApiKeyRepository(self.session)
        self.policy_rules = PolicyRuleRepository(self.session)
        self.approvals = ApprovalRepository(self.session)
        self.audit_logs = AuditLogRepository(self.session)
        self.eval_cases = EvalCaseRepository(self.session)
        self.eval_runs = EvalRunRepository(self.session)
        self.eval_scores = EvalScoreRepository(self.session)
        self.workspaces = WorkspaceRepository(self.session)
        self.tool_invocations = ToolInvocationRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        try:
            # 默认 rollback 是刻意的：只有显式 commit 才能持久化。后续 runtime
            # 错误路径、guardrail 拦截和 checkpoint 中断都依赖这个保守语义。
            if exc_type is not None or not self._committed:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        await self.session.rollback()
        self._committed = True


class SQLAlchemyStorage:
    """基于 SQLAlchemy async engine 的 Repository/UoW factory。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = normalize_async_dsn(dsn)
        self.engine: AsyncEngine = create_async_engine(self.dsn)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

    @classmethod
    def from_dsn(cls, dsn: str) -> SQLAlchemyStorage:
        return cls(dsn)

    @asynccontextmanager
    async def uow(self) -> AsyncGenerator[SQLAlchemyUnitOfWork]:
        unit = SQLAlchemyUnitOfWork(self._session_factory)
        async with unit:
            yield unit

    async def dispose(self) -> None:
        await self.engine.dispose()
