"""Run-scoped evidence 写入时的 canonical trace 门禁。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.run_trace import TRACE_ID_PATTERN, RunTraceError
from agent_harness.storage.models import AgentRunModel

if TYPE_CHECKING:
    from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage


class RunTraceScopeConflict(RunTraceError):
    """Evidence 声明的 run、tenant 或 trace 无法组成同一 canonical scope。"""

    def __init__(self) -> None:
        """构造统一的 409 scope 冲突，避免不同写入通道泄露底层 run 是否存在。"""

        super().__init__(
            "trace.scope_conflict",
            "run-scoped trace evidence does not match the persisted run",
            status_code=409,
        )


class RunTraceResolver(Protocol):
    """EventBus 与 local sink 使用的持久化 canonical trace 查询 seam。"""

    async def __call__(self, *, tenant_id: str, run_id: str) -> str:
        """按已认证 tenant 返回 run 的 canonical trace。"""
        ...


class StorageRunTraceResolver:
    """通过 storage UoW 实现 tenant-scoped run trace 解析。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        """保存 storage seam；每次解析自行开启短生命周期 UoW。"""

        self._storage = storage

    def __eq__(self, other: object) -> bool:
        """按 DSN 判定 resolver 是否等价，支持 composition root 的重复绑定保护。"""

        return (
            isinstance(other, StorageRunTraceResolver) and self._storage.dsn == other._storage.dsn
        )

    async def __call__(self, *, tenant_id: str, run_id: str) -> str:
        """在持久化边界解析 run 的 canonical trace，不信任调用方提供的 trace。"""

        async with self._storage.uow() as uow:
            return await canonical_trace_for_run(
                uow.session,
                tenant_id=tenant_id,
                run_id=run_id,
            )


async def canonical_trace_for_run(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: str,
) -> str:
    """按已认证 tenant 读取 run 的 canonical trace；损坏或越界记录一律拒绝。"""

    run = await session.scalar(
        select(AgentRunModel).where(
            AgentRunModel.id == run_id,
            AgentRunModel.tenant_id == tenant_id,
        )
    )
    if run is None or TRACE_ID_PATTERN.fullmatch(run.trace_id) is None:
        raise RunTraceScopeConflict
    return run.trace_id


async def require_canonical_run_trace(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: str,
    trace_id: str | None,
) -> str:
    """要求调用方投影值与已持久化 run trace 逐值一致。"""

    canonical = await canonical_trace_for_run(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
    )
    if trace_id != canonical:
        raise RunTraceScopeConflict
    return canonical


async def project_canonical_run_trace(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: str,
    trace_id: str | None,
) -> str:
    """缺失时从 persisted run 投影；非空覆盖值仍必须逐值一致。"""

    canonical = await canonical_trace_for_run(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
    )
    if trace_id is not None and trace_id != canonical:
        raise RunTraceScopeConflict
    return canonical
