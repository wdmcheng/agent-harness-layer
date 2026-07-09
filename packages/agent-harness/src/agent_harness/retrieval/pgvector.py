"""pgvector optional adapter capability seam。"""

from __future__ import annotations

from agent_harness.retrieval.postgres import PostgreSQLRetrievalProvider


class PGVectorRetrievalProvider(PostgreSQLRetrievalProvider):
    """pgvector 扩展存在时可参与 hybrid retrieval 的 adapter 占位。"""

    provider = "pgvector"
    required_extension = "vector"
