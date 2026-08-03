"""RAG Assistant 的输入与输出 schema，显式保留来源信任边界。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

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


class RagAssemblyTruncationEmpty(HarnessDTO):
    """没有创建 Context Assembly 时的唯一空摘要，canonical payload 固定为 ``{}``。"""


class RagAssemblyTruncation(HarnessDTO):
    """Context Assembly producer 已冻结的六个非负组裁计数。"""

    input_count: int = Field(ge=0, strict=True)
    retained_count: int = Field(ge=0, strict=True)
    truncated_count: int = Field(ge=0, strict=True)
    dropped_count: int = Field(ge=0, strict=True)
    used_tokens: int = Field(ge=0, strict=True)
    fragment_count: int = Field(ge=0, strict=True)


class RagOutput(HarnessDTO):
    """带 citation、assembly/model/trace evidence 的稳定输出。"""

    status: Literal["no_source", "completed"]
    answer: str
    citations: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    retrieval_provider: str
    assembly_id: str | None = None
    assembly_truncation: RagAssemblyTruncationEmpty | RagAssemblyTruncation = Field(
        default_factory=RagAssemblyTruncationEmpty
    )
    model_provider: str | None = None
    trust_level: str = "untrusted"
    trace_ref: str

    @model_validator(mode="after")
    def validate_status_evidence(self) -> RagOutput:
        """状态必须与来源、assembly identity 和互斥组裁变体逐值一致。"""

        if self.status == "no_source":
            if (
                not isinstance(self.assembly_truncation, RagAssemblyTruncationEmpty)
                or self.citations
                or self.source_refs
                or self.assembly_id is not None
                or self.model_provider is not None
            ):
                raise ValueError("no_source requires empty source and assembly evidence")
            return self
        if (
            not isinstance(self.assembly_truncation, RagAssemblyTruncation)
            or not self.citations
            or not self.source_refs
            or not self.assembly_id
            or not self.model_provider
        ):
            raise ValueError("completed requires source, assembly and model evidence")
        return self
