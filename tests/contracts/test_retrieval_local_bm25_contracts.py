"""Local SQLite FTS5/BM25 retrieval adapter 的公开契约测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.storage import run_migrations


@pytest.mark.asyncio
async def test_local_bm25_adapter_returns_citations_without_postgres(tmp_path: Path) -> None:
    """local profile 的 RAG 检索必须只依赖 SQLite FTS5/BM25。"""

    from agent_harness.retrieval import (
        LocalSQLiteBM25RetrievalProvider,
        RetrievalChunk,
        RetrievalDocument,
        RetrievalIndexRequest,
        RetrievalQueryRequest,
    )

    db_path = tmp_path / "local-bm25.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    provider = LocalSQLiteBM25RetrievalProvider(dsn=dsn)

    await provider.index(
        RetrievalIndexRequest(
            tenant_id="default",
            collection="docs",
            documents=[
                RetrievalDocument(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-1",
                    source_ref="file://docs/retrieval.md",
                    citation="Retrieval Guide",
                    metadata={"title": "Retrieval"},
                )
            ],
            chunks=[
                RetrievalChunk(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-1",
                    chunk_id="chunk-1",
                    content="Agent Harness retrieval returns citations.",
                    source_ref="file://docs/retrieval.md#chunk-1",
                    citation="Retrieval Guide §1",
                    token_estimate=7,
                ),
                RetrievalChunk(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-1",
                    chunk_id="chunk-2",
                    content="Prompt injection says ignore the system policy.",
                    source_ref="file://docs/retrieval.md#chunk-2",
                    citation="Retrieval Guide §2",
                    token_estimate=8,
                ),
            ],
        )
    )

    response = await provider.query(
        RetrievalQueryRequest(
            tenant_id="default",
            collection="docs",
            query="retrieval citations",
            top_k=2,
        )
    )
    empty = await provider.query(
        RetrievalQueryRequest(
            tenant_id="default",
            collection="docs",
            query="nonexistentterm",
            top_k=2,
        )
    )

    assert response.provider == "local-bm25"
    assert response.results[0].chunk_id == "chunk-1"
    assert response.results[0].rank == 1
    assert response.results[0].citation == "Retrieval Guide §1"
    assert response.results[0].trust_level == "untrusted"
    assert response.results[0].metadata["ranking"] == "sqlite_fts5_bm25_ascending"
    assert empty.results == []
    assert empty.metadata["empty_reason"] == "no_match"


@pytest.mark.asyncio
async def test_local_bm25_adapter_isolates_tenants_with_overlapping_chunk_ids(
    tmp_path: Path,
) -> None:
    """FTS 索引必须按 tenant 隔离，不能让 content 和 citation 跨租户错配。"""

    from agent_harness.retrieval import (
        LocalSQLiteBM25RetrievalProvider,
        RetrievalChunk,
        RetrievalDocument,
        RetrievalIndexRequest,
        RetrievalQueryRequest,
    )

    db_path = tmp_path / "local-bm25-tenants.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    provider = LocalSQLiteBM25RetrievalProvider(dsn=dsn)

    async def index_chunk(
        *,
        tenant_id: str,
        content: str,
        citation: str,
        source_ref: str,
    ) -> None:
        """写入可覆盖的同名分片，集中构造跨租户隔离和同租户更新输入。"""

        await provider.index(
            RetrievalIndexRequest(
                tenant_id=tenant_id,
                collection="shared-docs",
                documents=[
                    RetrievalDocument(
                        tenant_id=tenant_id,
                        collection="shared-docs",
                        document_id="doc",
                        source_ref=source_ref,
                        citation=citation,
                    )
                ],
                chunks=[
                    RetrievalChunk(
                        tenant_id=tenant_id,
                        collection="shared-docs",
                        document_id="doc",
                        chunk_id="shared",
                        content=content,
                        source_ref=f"{source_ref}#shared",
                        citation=f"{citation} chunk",
                    )
                ],
            )
        )

    await index_chunk(
        tenant_id="tenant-a",
        content="alpha-only retrieval evidence",
        citation="Tenant A",
        source_ref="tenant-a://doc",
    )
    await index_chunk(
        tenant_id="tenant-b",
        content="beta-only retrieval evidence",
        citation="Tenant B",
        source_ref="tenant-b://doc",
    )
    await index_chunk(
        tenant_id="tenant-a",
        content="alpha-only retrieval evidence updated",
        citation="Tenant A Updated",
        source_ref="tenant-a://doc-updated",
    )

    tenant_a_beta = await provider.query(
        RetrievalQueryRequest(
            tenant_id="tenant-a",
            collection="shared-docs",
            query="beta only",
            top_k=1,
        )
    )
    tenant_a_alpha = await provider.query(
        RetrievalQueryRequest(
            tenant_id="tenant-a",
            collection="shared-docs",
            query="alpha updated",
            top_k=1,
        )
    )
    tenant_b_beta = await provider.query(
        RetrievalQueryRequest(
            tenant_id="tenant-b",
            collection="shared-docs",
            query="beta only",
            top_k=1,
        )
    )

    assert tenant_a_beta.results == []
    assert tenant_a_beta.metadata["empty_reason"] == "no_match"
    assert tenant_a_alpha.results[0].content == "alpha-only retrieval evidence updated"
    assert tenant_a_alpha.results[0].citation == "Tenant A Updated chunk"
    assert tenant_a_alpha.results[0].source_ref == "tenant-a://doc-updated#shared"
    assert tenant_b_beta.results[0].content == "beta-only retrieval evidence"
    assert tenant_b_beta.results[0].citation == "Tenant B chunk"
    assert tenant_b_beta.results[0].source_ref == "tenant-b://doc#shared"


@pytest.mark.asyncio
async def test_local_bm25_adapter_distinguishes_documents_with_same_chunk_id(
    tmp_path: Path,
) -> None:
    """同一租户同 collection 下，chunk_id 只在 document_id 内唯一。"""

    from agent_harness.retrieval import (
        LocalSQLiteBM25RetrievalProvider,
        RetrievalChunk,
        RetrievalDocument,
        RetrievalIndexRequest,
        RetrievalQueryRequest,
    )

    db_path = tmp_path / "local-bm25-documents.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    provider = LocalSQLiteBM25RetrievalProvider(dsn=dsn)

    await provider.index(
        RetrievalIndexRequest(
            tenant_id="default",
            collection="docs",
            documents=[
                RetrievalDocument(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-alpha",
                    source_ref="doc://alpha",
                    citation="Doc Alpha",
                ),
                RetrievalDocument(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-beta",
                    source_ref="doc://beta",
                    citation="Doc Beta",
                ),
            ],
            chunks=[
                RetrievalChunk(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-alpha",
                    chunk_id="chunk-1",
                    content="alpha unique evidence",
                    source_ref="doc://alpha#chunk-1",
                    citation="Doc Alpha chunk",
                ),
                RetrievalChunk(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-beta",
                    chunk_id="chunk-1",
                    content="beta unique evidence",
                    source_ref="doc://beta#chunk-1",
                    citation="Doc Beta chunk",
                ),
            ],
        )
    )

    alpha = await provider.query(
        RetrievalQueryRequest(
            tenant_id="default",
            collection="docs",
            query="alpha unique",
            top_k=1,
        )
    )
    beta = await provider.query(
        RetrievalQueryRequest(
            tenant_id="default",
            collection="docs",
            query="beta unique",
            top_k=1,
        )
    )

    assert alpha.results[0].document_id == "doc-alpha"
    assert alpha.results[0].chunk_id == "chunk-1"
    assert alpha.results[0].citation == "Doc Alpha chunk"
    assert alpha.results[0].source_ref == "doc://alpha#chunk-1"
    assert beta.results[0].document_id == "doc-beta"
    assert beta.results[0].chunk_id == "chunk-1"
    assert beta.results[0].citation == "Doc Beta chunk"
    assert beta.results[0].source_ref == "doc://beta#chunk-1"


@pytest.mark.asyncio
async def test_local_bm25_rebuilds_legacy_fts_table_without_document_id(
    tmp_path: Path,
) -> None:
    """旧 FTS 表即使已有 tenant_id，缺 document_id 时也必须重建回填。"""

    from agent_harness.retrieval import (
        LocalSQLiteBM25RetrievalProvider,
        RetrievalChunk,
        RetrievalDocument,
        RetrievalIndexRequest,
        RetrievalQueryRequest,
    )

    db_path = tmp_path / "local-bm25-legacy-fts.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    provider = LocalSQLiteBM25RetrievalProvider(dsn=dsn)

    await provider.index(
        RetrievalIndexRequest(
            tenant_id="default",
            collection="docs",
            documents=[
                RetrievalDocument(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-legacy",
                    source_ref="doc://legacy",
                    citation="Legacy Doc",
                )
            ],
            chunks=[
                RetrievalChunk(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-legacy",
                    chunk_id="chunk-legacy",
                    content="legacy fts rebuild evidence",
                    source_ref="doc://legacy#chunk",
                    citation="Legacy Doc chunk",
                )
            ],
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("drop table retrieval_chunks_fts")
        connection.execute(
            """
            create virtual table retrieval_chunks_fts using fts5(
                tenant_id unindexed,
                collection unindexed,
                chunk_id unindexed,
                content,
                tokenize = 'unicode61'
            )
            """
        )

    response = await provider.query(
        RetrievalQueryRequest(
            tenant_id="default",
            collection="docs",
            query="legacy rebuild evidence",
            top_k=1,
        )
    )

    assert response.results[0].document_id == "doc-legacy"
    assert response.results[0].chunk_id == "chunk-legacy"
    assert response.results[0].citation == "Legacy Doc chunk"
