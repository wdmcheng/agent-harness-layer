"""检索证据仓储：隔离 ORM 模型并保存文档、分块、引用与信任元数据。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import RetrievalChunkModel, RetrievalDocumentModel


class RetrievalDocumentCreate(HarnessDTO):
    """创建 retrieval document 记录的公开输入。"""

    tenant_id: str
    collection: str
    document_id: str
    source_ref: str
    citation: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalDocumentRecord(RetrievalDocumentCreate):
    """已持久化 retrieval document 摘要。"""

    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RetrievalChunkCreate(HarnessDTO):
    """创建 retrieval chunk 记录的公开输入。"""

    tenant_id: str
    collection: str
    document_id: str
    chunk_id: str
    content: str
    content_ref: str | None = None
    source_ref: str
    citation: str
    trust_level: str = "untrusted"
    token_estimate: int = 0
    vector_ref: str | None = None
    rank_metadata: dict[str, Any] = Field(default_factory=dict)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalChunkRecord(RetrievalChunkCreate):
    """已持久化 retrieval chunk 摘要。"""

    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RetrievalDocumentRepository:
    """retrieval_documents 表 repository，调用方不直接接触 ORM model。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前工作单元的异步 session；提交时机始终由上层 UoW 控制。"""
        self._session = session

    async def create(self, data: RetrievalDocumentCreate) -> RetrievalDocumentRecord:
        """按租户、集合和业务文档 ID 幂等创建检索文档记录。

        重放索引请求时返回既有记录而不覆盖引用或元数据，避免后到的重复输入
        悄然改写已经用于回答证据链的来源描述。
        """
        existing = await self.get(
            tenant_id=data.tenant_id,
            collection=data.collection,
            document_id=data.document_id,
        )
        if existing is not None:
            return existing
        model = RetrievalDocumentModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            collection=data.collection,
            document_id=data.document_id,
            source_ref=data.source_ref,
            citation=data.citation,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _document_record(model)

    async def get(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
    ) -> RetrievalDocumentRecord | None:
        """按完整租户作用域读取文档，防止同名 document ID 跨集合或租户串用。"""
        result = await self._session.scalars(
            select(RetrievalDocumentModel).where(
                RetrievalDocumentModel.tenant_id == tenant_id,
                RetrievalDocumentModel.collection == collection,
                RetrievalDocumentModel.document_id == document_id,
            )
        )
        model = result.first()
        return None if model is None else _document_record(model)


class RetrievalChunkRepository:
    """retrieval_chunks 表 repository，保存 chunk、citation 和 rank evidence。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前工作单元的异步 session，保持 chunk 写入与外层事务同生共死。"""
        self._session = session

    async def create(self, data: RetrievalChunkCreate) -> RetrievalChunkRecord:
        """按文档内 chunk ID 幂等创建分块及其检索证据元数据。

        已存在的分块不做覆盖更新，确保 citation、信任等级和 Provider 排名依据
        在重放索引时保持稳定；需要更新内容必须走明确的迁移或版本化路径。
        """
        existing = await self.get(
            tenant_id=data.tenant_id,
            collection=data.collection,
            document_id=data.document_id,
            chunk_id=data.chunk_id,
        )
        if existing is not None:
            return existing
        model = RetrievalChunkModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            collection=data.collection,
            document_id=data.document_id,
            chunk_id=data.chunk_id,
            content=data.content,
            content_ref=data.content_ref,
            source_ref=data.source_ref,
            citation=data.citation,
            trust_level=data.trust_level,
            token_estimate=data.token_estimate,
            vector_ref=data.vector_ref,
            rank_metadata_json=data.rank_metadata,
            provider_metadata_json=data.provider_metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _chunk_record(model)

    async def get(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
        chunk_id: str,
    ) -> RetrievalChunkRecord | None:
        """按租户、集合、文档和分块四层键读取记录，保留严格隔离边界。"""
        result = await self._session.scalars(
            select(RetrievalChunkModel).where(
                RetrievalChunkModel.tenant_id == tenant_id,
                RetrievalChunkModel.collection == collection,
                RetrievalChunkModel.document_id == document_id,
                RetrievalChunkModel.chunk_id == chunk_id,
            )
        )
        model = result.first()
        return None if model is None else _chunk_record(model)


def _document_record(model: RetrievalDocumentModel) -> RetrievalDocumentRecord:
    """将 ORM 文档模型投影为 DTO，避免 session 生命周期泄漏到调用层。"""
    return RetrievalDocumentRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        collection=model.collection,
        document_id=model.document_id,
        source_ref=model.source_ref,
        citation=model.citation,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _chunk_record(model: RetrievalChunkModel) -> RetrievalChunkRecord:
    """将 ORM 分块模型投影为 DTO，并完整保留引用、信任和排序证据字段。"""
    return RetrievalChunkRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        collection=model.collection,
        document_id=model.document_id,
        chunk_id=model.chunk_id,
        content=model.content,
        content_ref=model.content_ref,
        source_ref=model.source_ref,
        citation=model.citation,
        trust_level=model.trust_level,
        token_estimate=model.token_estimate,
        vector_ref=model.vector_ref,
        rank_metadata=model.rank_metadata_json,
        provider_metadata=model.provider_metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
