"""跨调用存活的 embedding cache 事务适配层。"""

from __future__ import annotations

from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.repositories import EmbeddingCacheCreate, EmbeddingCacheRecord


class StorageEmbeddingCache:
    """每次 cache 操作自开 UoW，避免 provider 持有已关闭 session。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        """保存 storage seam；每次缓存操作独立提交，避免 provider 生命周期跨越 UoW。"""

        self._storage = storage

    async def get(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        """读取并提交 cache hit 计数等访问副作用，供实际 embedding 调用使用。"""

        async with self._storage.uow() as uow:
            record = await uow.embedding_cache.get(
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                input_hash=input_hash,
            )
            await uow.commit()
            return record

    async def peek(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        """为 shared-budget 预检提供不产生持久化写入的 cache lookup。"""

        async with self._storage.uow() as uow:
            return await uow.embedding_cache.peek(
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                input_hash=input_hash,
            )

    async def mark_hit(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord:
        """供非 settlement 调用在自己的事务中提交 cache hit evidence。"""

        async with self._storage.uow() as uow:
            record = await uow.embedding_cache.mark_hit(
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                input_hash=input_hash,
            )
            await uow.commit()
            return record

    async def put(self, data: EmbeddingCacheCreate) -> EmbeddingCacheRecord:
        """写入或更新 tenant-scoped embedding 缓存，并在返回前提交本次 UoW。"""

        async with self._storage.uow() as uow:
            record = await uow.embedding_cache.put(data)
            await uow.commit()
            return record


__all__ = ["StorageEmbeddingCache"]
