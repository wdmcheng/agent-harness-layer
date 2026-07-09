"""Retrieval evidence repository。"""

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
        self._session = session

    async def create(self, data: RetrievalDocumentCreate) -> RetrievalDocumentRecord:
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
        self._session = session

    async def create(self, data: RetrievalChunkCreate) -> RetrievalChunkRecord:
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
