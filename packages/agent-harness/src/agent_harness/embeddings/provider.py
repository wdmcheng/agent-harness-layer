"""Embedding provider interface 与 local/mock provider。"""

from __future__ import annotations

import hashlib
from typing import Protocol

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.repositories import EmbeddingCacheCreate, EmbeddingCacheRepository


class EmbeddingRequest(HarnessDTO):
    input: str
    tenant_id: str = "default"


class EmbeddingCacheInfo(HarnessDTO):
    hit: bool
    input_hash: str
    vector_ref: str


class EmbeddingResponse(HarnessDTO):
    provider: str
    model: str
    vector_ref: str
    vector: list[float]
    cache: EmbeddingCacheInfo


class EmbeddingProvider(Protocol):
    provider: str
    model: str

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """生成 embedding，并通过 provider 自己的 cache seam 报告 hit/miss。"""
        ...


class LocalEmbeddingProvider:
    """确定性 local embedding provider，用于测试和 CI。"""

    def __init__(
        self,
        *,
        cache: EmbeddingCacheRepository,
        provider: str = "local",
        model: str = "mock-small",
    ) -> None:
        self.provider = provider
        self.model = model
        self._cache = cache

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        input_hash = hashlib.sha256(request.input.encode("utf-8")).hexdigest()
        cached = await self._cache.get(
            provider=self.provider,
            model=self.model,
            input_hash=input_hash,
        )
        if cached is not None:
            return EmbeddingResponse(
                provider=self.provider,
                model=self.model,
                vector_ref=cached.vector_ref,
                vector=[],
                cache=EmbeddingCacheInfo(
                    hit=True,
                    input_hash=input_hash,
                    vector_ref=cached.vector_ref,
                ),
            )
        vector = _deterministic_vector(input_hash)
        vector_ref = f"embedding://{self.provider}/{self.model}/{input_hash}"
        await self._cache.put(
            EmbeddingCacheCreate(
                tenant_id=request.tenant_id,
                provider=self.provider,
                model=self.model,
                input_hash=input_hash,
                vector_ref=vector_ref,
                metadata={"latency_ms": 0, "dimensions": len(vector)},
            )
        )
        return EmbeddingResponse(
            provider=self.provider,
            model=self.model,
            vector_ref=vector_ref,
            vector=vector,
            cache=EmbeddingCacheInfo(hit=False, input_hash=input_hash, vector_ref=vector_ref),
        )


def _deterministic_vector(input_hash: str) -> list[float]:
    return [int(input_hash[index : index + 2], 16) / 255 for index in range(0, 8, 2)]
