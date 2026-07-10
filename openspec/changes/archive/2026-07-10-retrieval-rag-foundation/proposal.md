## Source Links

- Product-Spec.md: `REQ-012: 模型、预算、上下文组装与 embedding`、`REQ-013: Retrieval 与 RAG`、`6.3 数据规则`、`7. 外部依赖`、`11.2 工具与能力集`、`11.3 上下文与记忆`。
- DEV-PLAN.md: `Phase 9: RetrievalProvider 与 RAG 能力`、数据实体表中的 `retrieval_documents` / `retrieval_chunks`、风险表中的 PGroonga / pgvector optional adapter 和 prompt injection 风险。
- API-Contract.md: 本 change 不新增 HTTP endpoint；如后续增加 retrieval HTTP route，需先更新 API-Contract。
- Design-Brief.md 或设计稿：不适用，本 change 不涉及产品化前端 UI。
- CONTEXT.md / ADR: 未发现本轮必须读取的领域上下文或 ADR。

## Why

Phase 6 已建立 ContextAssembler 和 EmbeddingProvider，Phase 8 已把 tool/MCP output 标为不可信输入。现在需要把 RAG 检索纳入同一套 provider、storage、doctor、context trust 和示例验证边界，否则检索内容会绕过 source/citation/trust contract。

## What Changes

- 新增 `RetrievalProvider` 能力，统一 indexing、query、chunk metadata、citation、source_ref、trust_level 和 provider-neutral result DTO。
- 新增 local SQLite FTS5/BM25 adapter，保证 local profile 不依赖 PostgreSQL 扩展也能返回可排序检索结果。
- 新增 PostgreSQL lexical retrieval adapter，并为 PGroonga、pgvector 提供 optional adapter 探测、能力描述和 doctor 降级提示；扩展缺失时系统不崩溃。
- 新增 hybrid retrieval + RRF 合并接口，输出可追踪 ranking 证据。
- 新增 `retrieval_documents` 与 `retrieval_chunks` 持久化 evidence，并接入 SQLAlchemy UoW / repository seam。
- 新增 retrieval context DTO，把 chunk 转为 `ContextFragment(kind="retrieval", trust_level="untrusted")`，保留 citation/source_ref/truncation metadata；prompt injection 文本只能作为引用内容。
- 新增 RAG assistant 示例 config 和 approved eval 基础数据，用 fake/local 能力验证 citation 或未找到出处说明。

## Non-Goals

- 不实现 Phase 10 observability provider adapter、Phase 11 Eval Gate 或 trace->eval 自动闭环。
- 不新增 `/api/v1/retrieval`、`/api/v1/rag` 或其他 HTTP route。
- 不把 PGroonga、pgvector、OpenSearch、Elasticsearch、Vespa 设为 local profile 或 CI 必选依赖。
- 不把 Pydantic AI Harness 设为 Phase 9 必选依赖；本 change 只使用既有 `agent_harness` provider/context/storage seam。
- 不做完整 RAG 平台、文档管理 UI、自动长期个人记忆或线上 embedding provider 编排。

## Capabilities

### New Capabilities

- `retrieval-rag`

### Modified Capabilities

- 无。

## Impact

- 代码：新增 `agent_harness.retrieval` 包，扩展 storage models/repositories/UoW、migration、doctor diagnostics、template agent config。
- 契约：新增 OpenSpec delta spec 和 contract tests；不修改 API-Contract HTTP route。
- 数据：新增 `retrieval_documents`、`retrieval_chunks` 表；chunk 文本、citation、BM25/vector metadata 和 trust 信息通过 repository DTO 读写。
- 依赖：使用 SQLite FTS5、PostgreSQL native FTS；PGroonga/pgvector 仅作为 optional extension 探测和可选 adapter。
- 安全：retrieval chunk 默认 untrusted，注入上下文前必须保留 citation/source_ref/trust_level 和截断记录，指令型检索内容不得覆盖 system/policy/developer 指令。
