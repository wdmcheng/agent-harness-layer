"""Embedding provider interface 与 local/mock provider。"""

from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Protocol, runtime_checkable

from pydantic import field_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.repositories import EmbeddingCacheCreate, EmbeddingCacheRecord


class EmbeddingCacheStore(Protocol):
    """provider 可长期持有的 tenant-scoped cache seam。"""

    async def get(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        input_hash: str,
    ) -> EmbeddingCacheRecord | None:
        """读取 cache；实现负责提交 hit evidence。"""
        ...

    async def put(self, data: EmbeddingCacheCreate) -> EmbeddingCacheRecord:
        """持久化 miss 结果；实现负责事务边界。"""
        ...


class EmbeddingRequest(HarnessDTO):
    """一次 embedding 请求的稳定输入，tenant 用于 cache 证据归属。"""

    input: str
    tenant_id: str = "default"


class EmbeddingCacheInfo(HarnessDTO):
    """embedding cache 命中状态和可复用 vector 引用。"""

    hit: bool
    input_hash: str
    vector_ref: str

    @field_validator("hit", mode="before")
    @classmethod
    def validate_hit(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("embedding cache hit must be a boolean")
        return value


class EmbeddingResponse(HarnessDTO):
    """provider 生成或命中 cache 后返回的统一 embedding 结果。"""

    provider: str
    model: str
    vector_ref: str
    vector: list[float]
    cache: EmbeddingCacheInfo
    latency_ms: int = 0

    @field_validator("latency_ms", mode="before")
    @classmethod
    def validate_latency(cls, value: object) -> object:
        """adapter 边界拒绝会破坏 usage 聚合语义的无效延迟。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("embedding latency must be a non-negative integer")
        return value


class EmbeddingProvider(Protocol):
    """embedding adapter 的公共协议，屏蔽 HTTP/provider SDK 细节。"""

    provider: str
    model: str

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """生成 embedding，并通过 provider 自己的 cache seam 报告 hit/miss。"""
        ...


@runtime_checkable
class PreflightEmbeddingCacheProvider(EmbeddingProvider, Protocol):
    """允许 tenant-fenced 纯读 cache lookup 位于 shared reservation 之前。"""

    async def lookup_cache(self, request: EmbeddingRequest) -> EmbeddingResponse | None: ...

    async def embed_cache_miss(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


class LocalEmbeddingProvider:
    """确定性 local embedding provider，用于测试和 CI。"""

    def __init__(
        self,
        *,
        cache: EmbeddingCacheStore,
        provider: str = "local",
        model: str = "mock-small",
    ) -> None:
        self.provider = provider
        self.model = model
        self._cache = cache

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        cached = await self.lookup_cache(request)
        return cached if cached is not None else await self.embed_cache_miss(request)

    async def lookup_cache(self, request: EmbeddingRequest) -> EmbeddingResponse | None:
        """只读 tenant/provider/model/input hash，命中时不产生 provider 副作用。"""

        input_hash = hashlib.sha256(request.input.encode("utf-8")).hexdigest()
        started = perf_counter()
        cached = await self._cache.get(
            tenant_id=request.tenant_id,
            provider=self.provider,
            model=self.model,
            input_hash=input_hash,
        )
        if cached is not None:
            # cache hit 只返回 vector_ref，不重复携带向量正文，避免事件和 API payload 膨胀。
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
                latency_ms=int((perf_counter() - started) * 1000),
            )
        return None

    async def embed_cache_miss(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """调用方已完成 shared reservation 后才允许进入的 miss 写路径。"""

        input_hash = hashlib.sha256(request.input.encode("utf-8")).hexdigest()
        started = perf_counter()
        vector = _deterministic_vector(input_hash)
        latency_ms = int((perf_counter() - started) * 1000)
        tenant_hash = hashlib.sha256(request.tenant_id.encode("utf-8")).hexdigest()
        vector_ref = f"embedding://{self.provider}/{self.model}/{tenant_hash}/{input_hash}"
        await self._cache.put(
            EmbeddingCacheCreate(
                tenant_id=request.tenant_id,
                provider=self.provider,
                model=self.model,
                input_hash=input_hash,
                vector_ref=vector_ref,
                metadata={
                    "cache_status": "miss",
                    "vector_ref": vector_ref,
                    "provider_latency_status": "recorded",
                    "provider_latency_ms": latency_ms,
                    "dimensions": len(vector),
                },
            )
        )
        return EmbeddingResponse(
            provider=self.provider,
            model=self.model,
            vector_ref=vector_ref,
            vector=vector,
            cache=EmbeddingCacheInfo(hit=False, input_hash=input_hash, vector_ref=vector_ref),
            latency_ms=latency_ms,
        )


def _deterministic_vector(input_hash: str) -> list[float]:
    """从 input hash 派生短向量，保证测试和 smoke 不依赖真实 provider。"""

    return [int(input_hash[index : index + 2], 16) / 255 for index in range(0, 8, 2)]


__all__ = [
    "EmbeddingCacheInfo",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "LocalEmbeddingProvider",
    "PreflightEmbeddingCacheProvider",
]
