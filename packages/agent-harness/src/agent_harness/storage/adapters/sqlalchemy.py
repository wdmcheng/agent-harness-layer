"""SQLAlchemy async storage adapter 与 Unit of Work。"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from agent_harness.storage.delegation_repositories import DelegationRepository
from agent_harness.storage.eval_acceptance_repositories import HarnessAcceptanceRepository
from agent_harness.storage.eval_dataset_split_repositories import EvalDatasetSplitRepository
from agent_harness.storage.eval_experiment_repositories import (
    EvalExperimentRepository,
)
from agent_harness.storage.eval_repositories import (
    EvalCaseRepository,
    EvalRunRepository,
    EvalScoreRepository,
)
from agent_harness.storage.evidence_repositories import (
    EventCapacityRepository,
    EvidenceOutboxRepository,
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
from agent_harness.storage.settings import normalize_async_dsn, sqlite_database_path
from agent_harness.storage.shared_budget_repositories import SharedBudgetRepository
from agent_harness.storage.tool_repositories import (
    ToolInvocationRepository,
    WorkspaceRepository,
)


class SQLAlchemyUnitOfWork:
    """显式 commit 的事务边界；未 commit 离开时自动 rollback。

    一个工作单元只暴露绑定同一 ``AsyncSession`` 的仓储，避免调用方把 run、evidence、
    budget 或审计写入拆到彼此独立的事务中而失去原子性。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存 session 工厂并重置提交标记；真实 session 延迟到进入上下文时创建。"""

        self._session_factory = session_factory
        self._committed = False
        self.session: AsyncSession

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        """创建单一异步会话并装配所有仓储，使一次业务操作共享同一事务边界。"""

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
        self.eval_dataset_splits = EvalDatasetSplitRepository(self.session)
        self.eval_experiments = EvalExperimentRepository(self.session)
        self.harness_acceptance_records = HarnessAcceptanceRepository(self.session)
        self.workspaces = WorkspaceRepository(self.session)
        self.tool_invocations = ToolInvocationRepository(self.session)
        self.event_capacity = EventCapacityRepository(self.session)
        self.evidence_outbox = EvidenceOutboxRepository(self.session)
        self.delegations = DelegationRepository(self.session)
        self.shared_budget = SharedBudgetRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """异常或未显式提交时回滚，随后无条件关闭会话释放连接与事务资源。"""

        try:
            # 默认 rollback 是刻意的：只有显式 commit 才能持久化。后续 runtime
            # 错误路径、guardrail 拦截和 checkpoint 中断都依赖这个保守语义。
            if exc_type is not None or not self._committed:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        """提交当前会话并标记工作单元已完成，退出上下文时不再自动回滚。"""

        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        """显式回滚当前会话并标记该工作单元已处理，避免退出时重复回滚。"""

        await self.session.rollback()
        self._committed = True


class SQLAlchemyStorage:
    """基于 SQLAlchemy async engine 的仓储与工作单元工厂。

    除了创建共享会话，该适配层还为同一幂等范围提供与数据库方言匹配的串行锁；调用方
    不应绕开它自行混用连接池、advisory lock 或本地文件锁。
    """

    def __init__(self, dsn: str, *, cross_event_loop: bool = False) -> None:
        """规范化 DSN 并创建引擎；跨事件循环工作流选择 ``NullPool`` 隔离连接状态。"""

        self.dsn = normalize_async_dsn(dsn)
        # DBOS async durable step运行在独立event loop；NullPool避免把asyncpg
        # connection/Future跨loop复用。local/API-only路径继续用默认连接池。
        self.engine: AsyncEngine = create_async_engine(
            self.dsn,
            **({"poolclass": NullPool} if cross_event_loop else {}),
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        self._loop_idempotency_locks: WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
        ] = WeakKeyDictionary()
        self._loop_idempotency_locks_guard = threading.Lock()

    @classmethod
    def from_dsn(cls, dsn: str, *, cross_event_loop: bool = False) -> SQLAlchemyStorage:
        """提供统一构造入口，确保所有调用路径应用同一 DSN 规范化与连接池策略。"""

        return cls(dsn, cross_event_loop=cross_event_loop)

    @asynccontextmanager
    async def uow(self) -> AsyncGenerator[SQLAlchemyUnitOfWork]:
        """打开一个显式提交的工作单元；调用方离开上下文后不能继续使用其中仓储。"""

        unit = SQLAlchemyUnitOfWork(self._session_factory)
        async with unit:
            yield unit

    @asynccontextmanager
    async def idempotency_request_lock(self, scope: str) -> AsyncGenerator[None]:
        """跨 guardrail/runtime 事务串行化同一幂等范围。

        PostgreSQL 用 session advisory lock 跨进程协调，文件型 SQLite 用权限最小的锁文件，
        内存 SQLite 则只能在当前 storage 和事件循环内协调；三种实现都不暴露原始 scope。
        """

        digest = hashlib.sha256(scope.encode("utf-8")).digest()
        if self.engine.dialect.name == "postgresql":
            # Session-level advisory lock 跨越 prepare、policy/audit 与 run create 的
            # 多个事务；连接在 finally 中显式解锁，API 多进程也共享同一门禁。
            advisory_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
            async with self.engine.connect() as connection:
                await connection.execute(
                    text("select pg_advisory_lock(:lock_key)"),
                    {"lock_key": advisory_key},
                )
                try:
                    yield
                finally:
                    await connection.execute(
                        text("select pg_advisory_unlock(:lock_key)"),
                        {"lock_key": advisory_key},
                    )
            return

        sqlite_path = sqlite_database_path(self.dsn)
        if sqlite_path is not None:
            lock_path = sqlite_path.with_name(
                f".{sqlite_path.name}.idempotency-{digest.hex()[:24]}.lock"
            )
            descriptor = await asyncio.to_thread(_acquire_file_lock, lock_path)
            try:
                yield
            finally:
                await asyncio.to_thread(_release_file_lock, descriptor)
            return

        # In-memory SQLite 没有跨进程共享状态；按 event loop 共享 storage 内锁。
        loop = asyncio.get_running_loop()
        with self._loop_idempotency_locks_guard:
            locks = self._loop_idempotency_locks.setdefault(loop, {})
            lock = locks.setdefault(scope, asyncio.Lock())
        async with lock:
            yield

    async def dispose(self) -> None:
        """释放引擎持有的连接池资源；调用后不能再创建新的工作单元。"""

        await self.engine.dispose()


def _acquire_file_lock(path: Path) -> int:
    """在 SQLite 数据库同目录取得阻塞式排他锁，返回供释放函数关闭的文件描述符。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _release_file_lock(descriptor: int) -> None:
    """释放文件锁并始终关闭描述符，确保异常路径不会遗留本地幂等门禁。"""

    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
