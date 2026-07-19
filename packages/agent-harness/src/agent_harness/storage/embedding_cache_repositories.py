"""Tenant-scoped embedding cache DTO 与 repository。"""

from __future__ import annotations

from math import isfinite
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import EmbeddingCacheModel


class EmbeddingCacheCreate(HarnessDTO):
    """写入 tenant embedding cache 的隔离记录。"""

    tenant_id: str
    provider: str
    model: str
    input_hash: str
    vector_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingCacheRecord(EmbeddingCacheCreate):
    """已持久化的 embedding cache 记录。"""

    id: str


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


class EmbeddingCacheRepository:
    """EmbeddingProvider 专用的 tenant-scoped cache repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        """读取 tenant-scoped cache，并把持久化 outcome 更新为 hit。"""

        return await self._get(
            tenant_id=tenant_id,
            provider=provider,
            model=model,
            input_hash=input_hash,
            mark_hit=True,
        )

    async def peek(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        """只读 tenant-scoped cache，不在预检事务外提交 hit evidence。"""

        return await self._get(
            tenant_id=tenant_id,
            provider=provider,
            model=model,
            input_hash=input_hash,
            mark_hit=False,
        )

    async def mark_hit(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord:
        """在调用方 UoW 内提交 hit evidence，缺失记录视为完整性损坏。"""

        record = await self._get(
            tenant_id=tenant_id,
            provider=provider,
            model=model,
            input_hash=input_hash,
            mark_hit=True,
        )
        if record is None:
            raise LookupError("embedding cache hit record is missing")
        return record

    async def _get(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
        mark_hit: bool,
    ) -> EmbeddingCacheRecord | None:
        """执行隔离查询；put 的幂等探测不能被误记为消费命中。"""

        result = await self._session.scalars(
            select(EmbeddingCacheModel).where(
                EmbeddingCacheModel.tenant_id == tenant_id,
                EmbeddingCacheModel.provider == provider,
                EmbeddingCacheModel.model == model,
                EmbeddingCacheModel.input_hash == input_hash,
            )
        )
        row = result.first()
        if row is None:
            return None
        if mark_hit:
            metadata = dict(row.metadata_json)
            metadata["cache_status"] = "hit"
            row.metadata_json = metadata
            await self._session.flush()
        return _embedding_cache_record(row)

    async def put(self, data: EmbeddingCacheCreate) -> EmbeddingCacheRecord:
        """写入 embedding cache；已存在时返回既有 vector_ref。"""

        _validate_new_embedding_cache(data)
        existing = await self._get(
            tenant_id=data.tenant_id,
            provider=data.provider,
            model=data.model,
            input_hash=data.input_hash,
            mark_hit=False,
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


def _validate_new_embedding_cache(data: EmbeddingCacheCreate) -> None:
    """拒绝把 migration 专属 unavailable 状态写成新的 provider evidence。"""

    metadata = data.metadata
    latency = metadata.get("provider_latency_ms")
    if (
        metadata.get("provider_latency_status") != "recorded"
        or isinstance(latency, bool)
        or not isinstance(latency, int | float)
        or not isfinite(latency)
        or latency < 0
    ):
        raise ValueError("embedding cache provider latency must be recorded and non-negative")
    if metadata.get("cache_status") != "miss":
        raise ValueError("embedding cache new writes must start with cache_status=miss")
    if metadata.get("vector_ref") != data.vector_ref:
        raise ValueError("embedding cache metadata vector_ref must match the row")
