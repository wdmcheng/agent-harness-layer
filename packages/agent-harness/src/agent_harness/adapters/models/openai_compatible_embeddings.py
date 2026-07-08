"""OpenAI-compatible embedding adapter 边界。"""

from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, cast

import httpx

from agent_harness.embeddings.provider import (
    EmbeddingCacheInfo,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.storage.repositories import EmbeddingCacheCreate, EmbeddingCacheRepository


class OpenAICompatibleEmbeddingProvider:
    """调用兼容 OpenAI `/embeddings` 协议的 provider，并复用持久化 cache。"""

    provider = "openai-compatible"

    def __init__(
        self,
        *,
        cache: EmbeddingCacheRepository,
        base_url: str,
        model: str,
        api_key: str | None = None,
        provider: str = "openai-compatible",
        timeout_seconds: float = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._cache = cache
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """读取或创建 embedding vector ref，避免调用方处理 provider 原始响应。"""

        input_hash = hashlib.sha256(request.input.encode("utf-8")).hexdigest()
        cached = await self._cache.get(
            provider=self.provider,
            model=self.model,
            input_hash=input_hash,
        )
        if cached is not None:
            # 复用 cache 时只交还 vector_ref；调用方不需要再次处理 provider 原始响应。
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

        started = perf_counter()
        payload = await self._post_embedding(request.input)
        vector = _extract_embedding_vector(payload)
        latency_ms = int((perf_counter() - started) * 1000)
        vector_ref = f"embedding://{self.provider}/{self.model}/{input_hash}"
        await self._cache.put(
            EmbeddingCacheCreate(
                tenant_id=request.tenant_id,
                provider=self.provider,
                model=self.model,
                input_hash=input_hash,
                vector_ref=vector_ref,
                metadata={
                    "cache": "miss",
                    "latency_ms": latency_ms,
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
        )

    async def _post_embedding(self, input_text: str) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"model": self.model, "input": input_text}
        if self._client is not None:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json=body,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    json=body,
                    headers=headers,
                )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


def _extract_embedding_vector(payload: dict[str, Any]) -> list[float]:
    """把 OpenAI-compatible 响应收敛成内部向量列表。"""

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("embedding response is missing data[0].embedding")
    first = cast(object, data[0])
    if not isinstance(first, dict):
        raise ValueError("embedding response data[0] must be an object")
    first_payload = cast(dict[str, object], first)
    embedding_value = first_payload.get("embedding")
    if not isinstance(embedding_value, list):
        raise ValueError("embedding response data[0].embedding must be a list")
    vector: list[float] = []
    for value in cast(list[object], embedding_value):
        if not isinstance(value, int | float):
            raise ValueError("embedding vector values must be numeric")
        vector.append(float(value))
    return vector


_provider_contract: type[EmbeddingProvider] = OpenAICompatibleEmbeddingProvider
