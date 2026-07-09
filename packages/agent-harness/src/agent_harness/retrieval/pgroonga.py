"""PGroonga optional adapter capability seam。"""

from __future__ import annotations

from agent_harness.retrieval.postgres import PostgreSQLRetrievalProvider


class PGroongaRetrievalProvider(PostgreSQLRetrievalProvider):
    """PGroonga 扩展存在时可替换 native FTS 的 adapter 占位。"""

    provider = "pgroonga"
    required_extension = "pgroonga"
