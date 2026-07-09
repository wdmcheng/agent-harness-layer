"""Retrieval provider 公共 DTO 与协议。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class RetrievalDocument(HarnessDTO):
    """待索引文档的 provider-neutral metadata。"""

    tenant_id: str = "default"
    collection: str
    document_id: str
    source_ref: str
    citation: str
    metadata: dict[str, object] = Field(default_factory=dict)


class RetrievalChunk(HarnessDTO):
    """待索引 chunk，默认按不可信外部内容处理。"""

    tenant_id: str = "default"
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
    rank_metadata: dict[str, object] = Field(default_factory=dict)
    provider_metadata: dict[str, object] = Field(default_factory=dict)


def _empty_documents() -> list[RetrievalDocument]:
    return []


def _empty_chunks() -> list[RetrievalChunk]:
    return []


class RetrievalIndexRequest(HarnessDTO):
    """一次文档/chunk 索引请求。"""

    tenant_id: str = "default"
    collection: str
    documents: list[RetrievalDocument] = Field(default_factory=_empty_documents)
    chunks: list[RetrievalChunk] = Field(default_factory=_empty_chunks)


class RetrievalQueryRequest(HarnessDTO):
    """一次检索查询请求。"""

    tenant_id: str = "default"
    collection: str
    query: str
    top_k: int = 5
    vector: list[float] | None = None


class RetrievalResult(HarnessDTO):
    """单条 provider-neutral 检索结果。"""

    provider: str
    document_id: str
    chunk_id: str
    content: str
    score: float
    rank: int
    source_ref: str
    citation: str
    trust_level: str = "untrusted"
    metadata: dict[str, object] = Field(default_factory=dict)


class RetrievalResponse(HarnessDTO):
    """检索响应，metadata 保存 provider 路径和降级原因。"""

    provider: str
    query: str
    results: list[RetrievalResult]
    metadata: dict[str, object] = Field(default_factory=dict)


class RetrievalProvider(Protocol):
    """所有检索 adapter 必须实现的最小公共协议。"""

    provider: str

    async def index(self, request: RetrievalIndexRequest) -> None:
        """索引文档和 chunk，并保存必要 evidence。"""
        ...

    async def query(self, request: RetrievalQueryRequest) -> RetrievalResponse:
        """执行一次检索查询，返回 provider-neutral 结果。"""
        ...
