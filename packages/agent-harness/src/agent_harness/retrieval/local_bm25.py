"""SQLite FTS5/BM25 local retrieval adapter。"""

from __future__ import annotations

import json
import re
from typing import cast
from uuid import uuid4

import aiosqlite

from agent_harness.retrieval.provider import (
    RetrievalIndexRequest,
    RetrievalQueryRequest,
    RetrievalResponse,
    RetrievalResult,
)
from agent_harness.storage.settings import normalize_async_dsn, sqlite_database_path


class LocalSQLiteBM25RetrievalProvider:
    """基于同一个 SQLite 文件的 FTS5/BM25 adapter，用于 local/offline profile。"""

    provider = "local-bm25"

    def __init__(self, *, dsn: str) -> None:
        sqlite_path = sqlite_database_path(normalize_async_dsn(dsn))
        if sqlite_path is None:
            raise ValueError("LocalSQLiteBM25RetrievalProvider requires a file SQLite DSN")
        self._path = sqlite_path

    async def index(self, request: RetrievalIndexRequest) -> None:
        """写入 retrieval evidence，并同步 FTS5 virtual table。"""

        async with aiosqlite.connect(self._path) as connection:
            await _ensure_fts5(connection)
            for document in request.documents:
                await connection.execute(
                    """
                    insert into retrieval_documents
                    (id, tenant_id, collection, document_id, source_ref, citation, metadata_json)
                    values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(tenant_id, collection, document_id) do update set
                        source_ref = excluded.source_ref,
                        citation = excluded.citation,
                        metadata_json = excluded.metadata_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        str(uuid4()),
                        document.tenant_id,
                        document.collection,
                        document.document_id,
                        document.source_ref,
                        document.citation,
                        json.dumps(document.metadata, ensure_ascii=False),
                    ),
                )
            for chunk in request.chunks:
                await connection.execute(
                    """
                    insert into retrieval_chunks
                    (
                        id, tenant_id, collection, document_id, chunk_id, content, content_ref,
                        source_ref, citation, trust_level, token_estimate, vector_ref,
                        rank_metadata_json, provider_metadata_json
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(tenant_id, collection, document_id, chunk_id) do update set
                        content = excluded.content,
                        content_ref = excluded.content_ref,
                        source_ref = excluded.source_ref,
                        citation = excluded.citation,
                        trust_level = excluded.trust_level,
                        token_estimate = excluded.token_estimate,
                        vector_ref = excluded.vector_ref,
                        rank_metadata_json = excluded.rank_metadata_json,
                        provider_metadata_json = excluded.provider_metadata_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        str(uuid4()),
                        chunk.tenant_id,
                        chunk.collection,
                        chunk.document_id,
                        chunk.chunk_id,
                        chunk.content,
                        chunk.content_ref,
                        chunk.source_ref,
                        chunk.citation,
                        chunk.trust_level,
                        chunk.token_estimate,
                        chunk.vector_ref,
                        json.dumps(chunk.rank_metadata, ensure_ascii=False),
                        json.dumps(chunk.provider_metadata, ensure_ascii=False),
                    ),
                )
                await connection.execute(
                    """
                    delete from retrieval_chunks_fts
                    where tenant_id = ?
                      and collection = ?
                      and document_id = ?
                      and chunk_id = ?
                    """,
                    (chunk.tenant_id, chunk.collection, chunk.document_id, chunk.chunk_id),
                )
                await connection.execute(
                    """
                    insert into retrieval_chunks_fts(
                        tenant_id, collection, document_id, chunk_id, content
                    )
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.tenant_id,
                        chunk.collection,
                        chunk.document_id,
                        chunk.chunk_id,
                        chunk.content,
                    ),
                )
            await connection.commit()

    async def query(self, request: RetrievalQueryRequest) -> RetrievalResponse:
        """执行 FTS5 MATCH 查询，并按 SQLite bm25 升序转换 rank。"""

        match_query = _fts5_match_query(request.query)
        if not match_query:
            return RetrievalResponse(
                provider=self.provider,
                query=request.query,
                results=[],
                metadata={"empty_reason": "empty_query"},
            )
        async with aiosqlite.connect(self._path) as connection:
            await _ensure_fts5(connection)
            connection.row_factory = aiosqlite.Row
            rows = await connection.execute_fetchall(
                """
                select
                    c.document_id,
                    c.chunk_id,
                    c.content,
                    bm25(retrieval_chunks_fts) as bm25_score,
                    c.source_ref,
                    c.citation,
                    c.trust_level,
                    c.rank_metadata_json,
                    c.provider_metadata_json
                from retrieval_chunks_fts f
                join retrieval_chunks c
                  on c.tenant_id = f.tenant_id
                 and c.collection = f.collection
                 and c.document_id = f.document_id
                 and c.chunk_id = f.chunk_id
                where retrieval_chunks_fts match ?
                  and f.tenant_id = ?
                  and f.collection = ?
                  and c.tenant_id = ?
                order by bm25_score asc
                limit ?
                """,
                (
                    match_query,
                    request.tenant_id,
                    request.collection,
                    request.tenant_id,
                    request.top_k,
                ),
            )
        results = [_row_to_result(row, rank=index + 1) for index, row in enumerate(rows)]
        metadata: dict[str, object] = {
            "ranking": "sqlite_fts5_bm25_ascending",
            "match_query": match_query,
        }
        if not results:
            metadata["empty_reason"] = "no_match"
        return RetrievalResponse(
            provider=self.provider,
            query=request.query,
            results=results,
            metadata=metadata,
        )


async def _ensure_fts5(connection: aiosqlite.Connection) -> None:
    try:
        await _create_fts5(connection)
        columns = {
            str(row[1])
            for row in await connection.execute_fetchall("pragma table_info(retrieval_chunks_fts)")
        }
        expected_columns = {"tenant_id", "collection", "document_id", "chunk_id", "content"}
        if not expected_columns <= columns:
            # 早期开发库可能已经创建了缺 tenant_id 或 document_id 的 FTS 表。
            # 重建后从真相表回填，避免旧索引继续造成跨租户或跨文档错配。
            await connection.execute("drop table retrieval_chunks_fts")
            await _create_fts5(connection)
            await connection.execute(
                """
                insert into retrieval_chunks_fts(
                    tenant_id, collection, document_id, chunk_id, content
                )
                select tenant_id, collection, document_id, chunk_id, content
                from retrieval_chunks
                """
            )
            await connection.commit()
    except aiosqlite.Error as exc:
        raise RuntimeError("sqlite FTS5 is not available for local retrieval") from exc


async def _create_fts5(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        """
        create virtual table if not exists retrieval_chunks_fts using fts5(
            tenant_id unindexed,
            collection unindexed,
            document_id unindexed,
            chunk_id unindexed,
            content,
            tokenize = 'unicode61'
        )
        """
    )


def _fts5_match_query(query: str) -> str:
    terms = re.findall(r"[\w\u0080-\uffff]+", query.lower())
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _row_to_result(row: aiosqlite.Row, *, rank: int) -> RetrievalResult:
    rank_metadata = _load_json(row["rank_metadata_json"])
    provider_metadata = _load_json(row["provider_metadata_json"])
    metadata: dict[str, object] = {
        **rank_metadata,
        **provider_metadata,
        "bm25": row["bm25_score"],
        "ranking": "sqlite_fts5_bm25_ascending",
    }
    return RetrievalResult(
        provider="local-bm25",
        document_id=row["document_id"],
        chunk_id=row["chunk_id"],
        content=row["content"],
        score=float(row["bm25_score"]),
        rank=rank,
        source_ref=row["source_ref"],
        citation=row["citation"],
        trust_level=row["trust_level"],
        metadata=metadata,
    )


def _load_json(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        return {}
    loaded = json.loads(value)
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
