"""Retrieval/RAG OpenSpec、migration 和 repository 公开契约测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.storage import SQLAlchemyStorage, run_migrations

ROOT = Path(__file__).resolve().parents[2]
CHANGE = ROOT / "openspec" / "changes" / "retrieval-rag-foundation"


def test_openspec_declares_retrieval_rag_scope() -> None:
    """OpenSpec delta 必须覆盖 retrieval/RAG 行为，且明确不新增 HTTP route。"""

    proposal = (CHANGE / "proposal.md").read_text(encoding="utf-8")
    spec = (CHANGE / "specs" / "retrieval-rag" / "spec.md").read_text(encoding="utf-8")
    tasks = (CHANGE / "tasks.md").read_text(encoding="utf-8")

    for marker in [
        "RetrievalProvider",
        "SQLite FTS5/BM25",
        "PGroonga",
        "pgvector",
        "hybrid retrieval + RRF",
        "retrieval_documents",
        "retrieval_chunks",
        "RAG assistant",
    ]:
        assert marker in proposal or marker in spec
    assert "不新增 `/api/v1/retrieval`" in proposal
    assert "Pydantic AI Harness" in proposal
    assert "必选依赖" in proposal
    assert "openspec validate retrieval-rag-foundation --type change --strict" in tasks


def test_local_migration_creates_retrieval_tables(tmp_path: Path) -> None:
    """SQLite migration 是 local profile 的最低证据，不替代 PostgreSQL service smoke。"""

    db_path = tmp_path / "retrieval.db"
    run_migrations(sqlite_dsn(db_path))

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert {"retrieval_documents", "retrieval_chunks"} <= tables
    assert revision == ("0008_agent_execution_approval_claims",)


@pytest.mark.asyncio
async def test_retrieval_repository_round_trip(tmp_path: Path) -> None:
    """Repository/UoW 是 retrieval evidence 的公开持久化 seam。"""

    from agent_harness.storage import RetrievalChunkCreate, RetrievalDocumentCreate

    db_path = tmp_path / "retrieval-repository.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("default")
            document = await uow.retrieval_documents.create(
                RetrievalDocumentCreate(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-1",
                    source_ref="file://docs/guide.md",
                    citation="Guide §1",
                    metadata={"title": "Guide"},
                )
            )
            chunk = await uow.retrieval_chunks.create(
                RetrievalChunkCreate(
                    tenant_id="default",
                    collection="docs",
                    document_id=document.document_id,
                    chunk_id="chunk-1",
                    content="Agent Harness supports retrieval citations.",
                    source_ref=document.source_ref,
                    citation=document.citation,
                    trust_level="untrusted",
                    token_estimate=6,
                    vector_ref="embedding://local/mock/chunk-1",
                    rank_metadata={"bm25": -0.1},
                    provider_metadata={"provider": "local-bm25"},
                )
            )
            await uow.commit()

        async with storage.uow() as uow:
            loaded_doc = await uow.retrieval_documents.get(
                tenant_id="default",
                collection="docs",
                document_id="doc-1",
            )
            loaded_chunk = await uow.retrieval_chunks.get(
                tenant_id="default",
                collection="docs",
                document_id="doc-1",
                chunk_id="chunk-1",
            )
            other_document = await uow.retrieval_documents.create(
                RetrievalDocumentCreate(
                    tenant_id="default",
                    collection="docs",
                    document_id="doc-2",
                    source_ref="file://docs/other.md",
                    citation="Other §1",
                    metadata={"title": "Other"},
                )
            )
            other_chunk = await uow.retrieval_chunks.create(
                RetrievalChunkCreate(
                    tenant_id="default",
                    collection="docs",
                    document_id=other_document.document_id,
                    chunk_id="chunk-1",
                    content="Same chunk id in another document remains distinct.",
                    source_ref=other_document.source_ref,
                    citation=other_document.citation,
                    trust_level="untrusted",
                )
            )
            await uow.commit()
    finally:
        await storage.dispose()

    assert loaded_doc is not None
    assert loaded_doc.citation == "Guide §1"
    assert loaded_chunk is not None
    assert loaded_chunk.content == chunk.content
    assert loaded_chunk.trust_level == "untrusted"
    assert loaded_chunk.vector_ref == "embedding://local/mock/chunk-1"
    assert other_chunk.document_id == "doc-2"
    assert other_chunk.chunk_id == "chunk-1"
    assert other_chunk.citation == "Other §1"
