"""RAG Assistant 的输入与输出 schema，显式保留来源信任边界。"""

from __future__ import annotations

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class RagDocument(HarnessDTO):
    """local/fake 示例可索引的单个不可信来源。"""

    document_id: str
    content: str
    source_ref: str
    citation: str


def _empty_documents() -> list[RagDocument]:
    """为每次输入创建独立的空文档列表，避免 fixture 在请求之间共享可变状态。"""
    return []


class RagInput(HarnessDTO):
    """RAG query、预算和可选本地 fixture。"""

    query: str
    collection: str = "example-rag"
    top_k: int = Field(default=3, ge=1, le=20)
    token_budget: int = Field(default=256, ge=1)
    documents: list[RagDocument] = Field(default_factory=_empty_documents)


class RagOutput(HarnessDTO):
    """带 citation、assembly/model/trace evidence 的稳定输出。"""

    status: str
    answer: str
    citations: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    retrieval_provider: str
    assembly_id: str | None = None
    assembly_truncation: dict[str, int] = Field(default_factory=dict)
    model_provider: str | None = None
    trust_level: str = "untrusted"
    trace_ref: str
