"""PostgreSQL native full-text retrieval adapter。"""

from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_harness.retrieval.provider import (
    RetrievalIndexRequest,
    RetrievalQueryRequest,
    RetrievalResponse,
    RetrievalResult,
)
from agent_harness.storage.settings import normalize_async_dsn


class PostgreSQLRetrievalProvider:
    """使用 PostgreSQL native FTS 的 service profile fallback adapter。"""

    provider = "postgres-native-fts"

    def __init__(self, *, dsn: str) -> None:
        """规范化 async PostgreSQL DSN；每次操作临时建 engine，避免跨请求连接泄漏。"""

        self._dsn = normalize_async_dsn(dsn)

    async def index(self, request: RetrievalIndexRequest) -> None:
        """在单个 PostgreSQL 事务内 upsert 文档与 chunk，保持 collection/tenant 隔离键。

        chunk 的 conflict key 包含 document 与 chunk id；重建索引不会生成重复行，且
        engine 无论成功失败都会释放，避免 service worker 反复调用耗尽连接。
        """

        engine = create_async_engine(self._dsn)
        try:
            async with engine.begin() as connection:
                for document in request.documents:
                    await connection.execute(
                        text(
                            """
                            insert into retrieval_documents
                            (id, tenant_id, collection, document_id, source_ref, citation,
                             metadata_json, created_at, updated_at)
                            values (:id, :tenant_id, :collection,
                                    :document_id, :source_ref, :citation,
                                    cast(:metadata as json), now(), now())
                            on conflict (tenant_id, collection, document_id) do update set
                                source_ref = excluded.source_ref,
                                citation = excluded.citation,
                                metadata_json = excluded.metadata_json,
                                updated_at = now()
                            """
                        ),
                        {
                            "id": str(uuid4()),
                            "tenant_id": document.tenant_id,
                            "collection": document.collection,
                            "document_id": document.document_id,
                            "source_ref": document.source_ref,
                            "citation": document.citation,
                            "metadata": json.dumps(document.metadata, ensure_ascii=False),
                        },
                    )
                for chunk in request.chunks:
                    await connection.execute(
                        text(
                            """
                            insert into retrieval_chunks
                            (
                                id, tenant_id, collection, document_id, chunk_id, content,
                                content_ref, source_ref, citation, trust_level, token_estimate,
                                vector_ref, rank_metadata_json, provider_metadata_json,
                                created_at, updated_at
                            )
                            values (
                                :id, :tenant_id, :collection, :document_id,
                                :chunk_id, :content, :content_ref, :source_ref, :citation,
                                :trust_level, :token_estimate, :vector_ref,
                                cast(:rank_metadata as json), cast(:provider_metadata as json),
                                now(), now()
                            )
                            on conflict (tenant_id, collection, document_id, chunk_id) do update set
                                content = excluded.content,
                                content_ref = excluded.content_ref,
                                source_ref = excluded.source_ref,
                                citation = excluded.citation,
                                trust_level = excluded.trust_level,
                                token_estimate = excluded.token_estimate,
                                vector_ref = excluded.vector_ref,
                                rank_metadata_json = excluded.rank_metadata_json,
                                provider_metadata_json = excluded.provider_metadata_json,
                                updated_at = now()
                            """
                        ),
                        {
                            "id": str(uuid4()),
                            "tenant_id": chunk.tenant_id,
                            "collection": chunk.collection,
                            "document_id": chunk.document_id,
                            "chunk_id": chunk.chunk_id,
                            "content": chunk.content,
                            "content_ref": chunk.content_ref,
                            "source_ref": chunk.source_ref,
                            "citation": chunk.citation,
                            "trust_level": chunk.trust_level,
                            "token_estimate": chunk.token_estimate,
                            "vector_ref": chunk.vector_ref,
                            "rank_metadata": json.dumps(chunk.rank_metadata, ensure_ascii=False),
                            "provider_metadata": json.dumps(
                                chunk.provider_metadata,
                                ensure_ascii=False,
                            ),
                        },
                    )
        finally:
            await engine.dispose()

    async def query(self, request: RetrievalQueryRequest) -> RetrievalResponse:
        """使用 ``plainto_tsquery`` 执行租户与集合范围内的全文查询并返回稳定排名。"""

        engine = create_async_engine(self._dsn)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        select
                            document_id,
                            chunk_id,
                            content,
                            source_ref,
                            citation,
                            trust_level,
                            ts_rank_cd(
                                to_tsvector('simple', content),
                                plainto_tsquery('simple', :query)
                            ) as score
                        from retrieval_chunks
                        where tenant_id = :tenant_id
                          and collection = :collection
                          and to_tsvector('simple', content)
                              @@ plainto_tsquery('simple', :query)
                        order by score desc
                        limit :top_k
                        """
                    ),
                    {
                        "tenant_id": request.tenant_id,
                        "collection": request.collection,
                        "query": request.query,
                        "top_k": request.top_k,
                    },
                )
                rows = result.mappings().all()
        finally:
            await engine.dispose()
        results = [
            RetrievalResult(
                provider=self.provider,
                document_id=str(row["document_id"]),
                chunk_id=str(row["chunk_id"]),
                content=str(row["content"]),
                score=float(row["score"]),
                rank=index + 1,
                source_ref=str(row["source_ref"]),
                citation=str(row["citation"]),
                trust_level=str(row["trust_level"]),
                metadata={"ranking": "postgres_native_fts"},
            )
            for index, row in enumerate(rows)
        ]
        metadata: dict[str, object] = {"ranking": "postgres_native_fts"}
        if not results:
            metadata["empty_reason"] = "no_match"
        return RetrievalResponse(
            provider=self.provider,
            query=request.query,
            results=results,
            metadata=metadata,
        )
