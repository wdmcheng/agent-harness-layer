"""跨调用存活的 embedding cache 事务适配层。"""

from __future__ import annotations

from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.repositories import EmbeddingCacheCreate, EmbeddingCacheRecord


class StorageEmbeddingCache:
    """每次 cache 操作自开 UoW，避免 provider 持有已关闭 session。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def get(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        async with self._storage.uow() as uow:
            record = await uow.embedding_cache.get(
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                input_hash=input_hash,
            )
            await uow.commit()
            return record

    async def put(self, data: EmbeddingCacheCreate) -> EmbeddingCacheRecord:
        async with self._storage.uow() as uow:
            record = await uow.embedding_cache.put(data)
            await uow.commit()
            return record


__all__ = ["StorageEmbeddingCache"]
