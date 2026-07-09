## 1. 契约与测试先行

- [x] 1.1 新增 Phase 9 contract tests，覆盖 `retrieval-rag` OpenSpec 条目、provider DTO、local BM25、RRF、context injection、安全边界、doctor optional extension 降级和 RAG 示例 fixture。
- [x] 1.2 更新 storage migration tests 预期，先让 `retrieval_documents`、`retrieval_chunks` 和新 revision 断言失败。

## 2. Retrieval provider 与 context seam

- [x] 2.1 新增 `agent_harness.retrieval` DTO / Protocol / exports，公开 `RetrievalProvider`、index/query request/response、document/chunk/result DTO。
- [x] 2.2 新增 retrieval context helper，把检索结果转换成带 citation/source_ref/trust_level 的 `ContextFragment(kind="retrieval")`，并覆盖 prompt injection 文本只作为引用内容。

## 3. 持久化与 migration

- [x] 3.1 新增 `retrieval_documents`、`retrieval_chunks` ORM models、repository DTO、repository/UoW/export seam。
- [x] 3.2 新增 Alembic migration `0005_retrieval_rag_foundation` 和 `0006_retrieval_chunk_identity`，覆盖 SQLite schema、chunk document identity 和 PostgreSQL service migration 证据。

## 4. Local 与 service retrieval adapters

- [x] 4.1 实现 local SQLite FTS5/BM25 adapter，支持 index/query/empty result，并保留 citation/source_ref/trust metadata。
- [x] 4.2 实现 PostgreSQL native FTS adapter，提供 service profile lexical fallback。
- [x] 4.3 实现 PGroonga 与 pgvector optional adapter capability probe，不把扩展缺失当作启动失败。

## 5. Hybrid ranking 与 doctor

- [x] 5.1 实现 hybrid RRF merge interface，覆盖重复 chunk 去重、provider contribution 和最终 rank。
- [x] 5.2 扩展 `agent-harness doctor` 输出 PGroonga/pgvector installed/available/missing 状态和降级提示；local profile 不要求扩展。

## 6. RAG assistant 示例与收口验证

- [x] 6.1 新增 `templates/service-app/agents/examples/rag_assistant/config.yaml` 和 `evals/approved.yaml`，registry 可加载，eval fixture 覆盖 citation 与 no-source。
- [x] 6.2 跑 `openspec validate retrieval-rag-foundation --type change --strict`、Phase 9 targeted tests、`uv run pytest`、`make quality`、`uv run python scripts/smoke_local.py`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files`。
- [x] 6.3 通过 code-reviewer Stage 1/2 后同步 DEV-PLAN Phase 9 状态，写入 `.agents/.needs-review=clean`，并准备本地提交。
