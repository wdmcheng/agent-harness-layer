"""RetrievalProvider 与 RAG public seam。"""

from __future__ import annotations

from agent_harness.retrieval.context import (
    retrieval_result_to_context_fragment as retrieval_result_to_context_fragment,
)
from agent_harness.retrieval.context import (
    retrieval_results_to_context_fragments as retrieval_results_to_context_fragments,
)
from agent_harness.retrieval.hybrid import merge_rrf as merge_rrf
from agent_harness.retrieval.local_bm25 import (
    LocalSQLiteBM25RetrievalProvider as LocalSQLiteBM25RetrievalProvider,
)
from agent_harness.retrieval.pgroonga import PGroongaRetrievalProvider as PGroongaRetrievalProvider
from agent_harness.retrieval.pgvector import PGVectorRetrievalProvider as PGVectorRetrievalProvider
from agent_harness.retrieval.postgres import (
    PostgreSQLRetrievalProvider as PostgreSQLRetrievalProvider,
)
from agent_harness.retrieval.provider import RetrievalChunk as RetrievalChunk
from agent_harness.retrieval.provider import RetrievalDocument as RetrievalDocument
from agent_harness.retrieval.provider import RetrievalIndexRequest as RetrievalIndexRequest
from agent_harness.retrieval.provider import RetrievalProvider as RetrievalProvider
from agent_harness.retrieval.provider import RetrievalQueryRequest as RetrievalQueryRequest
from agent_harness.retrieval.provider import RetrievalResponse as RetrievalResponse
from agent_harness.retrieval.provider import RetrievalResult as RetrievalResult

__all__ = [
    "LocalSQLiteBM25RetrievalProvider",
    "PGroongaRetrievalProvider",
    "PGVectorRetrievalProvider",
    "PostgreSQLRetrievalProvider",
    "RetrievalChunk",
    "RetrievalDocument",
    "RetrievalIndexRequest",
    "RetrievalProvider",
    "RetrievalQueryRequest",
    "RetrievalResponse",
    "RetrievalResult",
    "merge_rrf",
    "retrieval_result_to_context_fragment",
    "retrieval_results_to_context_fragments",
]
