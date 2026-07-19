"""Context assembly 与 embedding cache/provider 合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_harness.storage import SQLAlchemyStorage, run_migrations


def sqlite_dsn(path: Path) -> str:
    """为上下文与 embedding 合同创建隔离异步 SQLite DSN，避免缓存结果跨用例复用。"""

    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_context_assembly_and_embedding_cache_are_persisted(tmp_path: Path) -> None:
    """上下文截断决策与本地 embedding 缓存必须持久化，使后续调用可复现保留内容与缓存命中。"""

    from agent_harness.context import ContextAssembler, ContextFragment
    from agent_harness.embeddings import EmbeddingRequest, LocalEmbeddingProvider

    db_path = tmp_path / "context-embedding.db"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    try:
        async with storage.uow() as uow:
            assembly = await ContextAssembler(uow.context_assemblies).assemble(
                tenant_id="default",
                run_id="run-context",
                fragments=[
                    ContextFragment(
                        source_ref="history:1",
                        trust_level="trusted",
                        content="short history",
                        token_estimate=2,
                        kind="history",
                    ),
                    ContextFragment(
                        source_ref="tool:1",
                        trust_level="untrusted",
                        content="tool output " * 20,
                        token_estimate=40,
                        kind="tool_output",
                    ),
                ],
                token_budget=10,
                output_ref="context://run-context/1",
            )
            first = await LocalEmbeddingProvider(
                cache=uow.embedding_cache,
                provider="local",
                model="mock-small",
            ).embed(EmbeddingRequest(input="repeat me"))
            await uow.commit()

        async with storage.uow() as uow:
            stored = await uow.context_assemblies.get(assembly.id)
            second = await LocalEmbeddingProvider(
                cache=uow.embedding_cache,
                provider="local",
                model="mock-small",
            ).embed(EmbeddingRequest(input="repeat me"))
    finally:
        await storage.dispose()

    assert stored is not None
    assert stored.token_budget == 10
    assert stored.output_ref == "context://run-context/1"
    assert stored.truncation_summary["truncated_count"] == 1
    assert stored.truncation_summary["dropped_count"] == 1
    assert stored.truncation_summary["fragment_count"] == 2
    assert assembly.fragment_traces[0].source_ref == "history:1"
    assert assembly.fragment_traces[0].status == "dropped"
    assert assembly.fragment_traces[0].retained_tokens == 0
    assert assembly.fragment_traces[1].source_ref == "tool:1"
    assert assembly.fragment_traces[1].status == "truncated"
    assert assembly.fragment_traces[1].trust_level == "untrusted"
    assert assembly.fragment_traces[1].retained_tokens == 10
    assert assembly.fallback_decision == "trimmed"
    assert first.cache.hit is False
    assert second.cache.hit is True
    assert second.vector_ref == first.vector_ref


@pytest.mark.asyncio
async def test_openai_compatible_embedding_adapter_posts_and_reuses_cache(tmp_path: Path) -> None:
    """OpenAI 兼容 adapter 只在首次 miss 发起受控 HTTP 请求，重试复用缓存且不暴露向量正文。"""

    from agent_harness.adapters.models.openai_compatible_embeddings import (
        OpenAICompatibleEmbeddingProvider,
    )
    from agent_harness.embeddings import EmbeddingRequest

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 adapter 发出的 HTTP 形状并返回固定向量，隔离网络与真实 API key。"""

        calls.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    db_path = tmp_path / "openai-compatible-embedding.db"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    try:
        async with storage.uow() as uow:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleEmbeddingProvider(
                    cache=uow.embedding_cache,
                    base_url="https://embedding.example/v1",
                    model="text-embedding-3-small",
                    api_key="test-key",
                    client=client,
                )
                first = await provider.embed(EmbeddingRequest(input="repeat me"))
                second = await provider.embed(EmbeddingRequest(input="repeat me"))
            await uow.commit()
    finally:
        await storage.dispose()

    assert calls == [
        {
            "url": "https://embedding.example/v1/embeddings",
            "authorization": "Bearer test-key",
            "body": {"model": "text-embedding-3-small", "input": "repeat me"},
        }
    ]
    assert first.provider == "openai-compatible"
    assert first.model == "text-embedding-3-small"
    assert first.vector == [0.1, 0.2, 0.3]
    assert first.cache.hit is False
    assert second.cache.hit is True
    assert second.vector == []
    assert second.vector_ref == first.vector_ref


@pytest.mark.asyncio
async def test_storage_embedding_cache_survives_provider_composition_uow_boundaries(
    tmp_path: Path,
) -> None:
    """StorageEmbeddingCache 跨独立 UoW 仍应保持命中。

    避免 provider composition 退化为进程内缓存。
    """

    from agent_harness.embeddings import (
        EmbeddingRequest,
        LocalEmbeddingProvider,
        StorageEmbeddingCache,
    )

    db_path = tmp_path / "storage-backed-embedding.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.commit()
        provider = LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage))

        first = await provider.embed(EmbeddingRequest(input="repeat me", tenant_id="tenant-a"))
        second = await provider.embed(EmbeddingRequest(input="repeat me", tenant_id="tenant-a"))

        assert first.cache.hit is False
        assert second.cache.hit is True
        assert second.vector_ref == first.vector_ref
        assert second.vector == []
    finally:
        await storage.dispose()
