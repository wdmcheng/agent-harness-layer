## 上下文

Phase 6 已落地 `ContextAssembler`、`EmbeddingProvider` 和 embedding cache；Phase 8 已落地 ToolRegistry、output guard、tool/MCP output 的 `source_ref` / `trust_level` / truncation metadata。Phase 9 不需要重写这些接缝，而是把 retrieval chunk 作为新的外部输入来源接入同一条上下文与持久化链路。

官方资料核验结论：
- SQLite FTS5 提供 virtual table 和 `bm25()` auxiliary function；`bm25()` 数值越小匹配越好，因此 local adapter 要按升序取结果并转换稳定 rank。
- PostgreSQL native FTS 用 `tsvector` / `tsquery` 和 rank 函数实现排序，可作为 service profile 的 lexical fallback。
- PGroonga 是 PostgreSQL extension，可提供 CJK/multilingual full text search；`pgroonga_score` 可按精确度排序，但本 change 只把它作为 optional adapter。
- pgvector 是 PostgreSQL vector similarity extension；支持 L2、inner product、cosine distance 等 operator，但本 change 只做 optional adapter seam 和 hybrid 输入，不强制安装。
- PostgreSQL `pg_extension` / `pg_available_extensions` 可用于 doctor 检测 extension installed/available 状态。

## 目标 / 非目标

**目标：**
- 建立 `agent_harness.retrieval` provider-neutral public seam。
- 用 SQLite FTS5/BM25 支撑 local/offline RAG 检索。
- 用 PostgreSQL native FTS 支撑 service fallback，并对 PGroonga/pgvector 输出 doctor 降级提示。
- 用 RRF 合并多 retriever 结果并保留 ranking 证据。
- 持久化 retrieval document/chunk evidence。
- 把检索结果转换为 `ContextFragment(kind="retrieval", trust_level="untrusted")`。
- 提供 RAG assistant 示例 config 和 approved eval 基础数据。

**非目标：**
- 不做 Phase 10+ observability/eval provider adapter。
- 不新增 HTTP route。
- 不强制安装 PGroonga、pgvector 或外部搜索服务。
- 不引入 Pydantic AI Harness 作为必选依赖。

## 设计决策

1. **RetrievalProvider 使用 DTO 和 Protocol，不暴露 provider row。**  
   `RetrievalRequest`、`RetrievalResult`、`RetrievalDocument`、`RetrievalChunk` 和 `RetrievalResponse` 作为公共契约。SQLite/PostgreSQL row、PGroonga score 或 pgvector operator 细节留在 adapter 内。替代方案是让调用方直接执行 SQL；拒绝，因为会绕过 storage/context trust contract。

2. **Local adapter 直接管理 FTS5 virtual table，但 evidence 仍写 repository。**  
   SQLite FTS5 查询最好直接穿过同一个 SQLite database connection；adapter 负责创建/维护 FTS virtual table 和 doc/chunk evidence。repository/UoW 负责普通 evidence 表，避免业务入口碰 ORM session。替代方案是只做 Python 内存 BM25；拒绝，因为 local profile 需要真实 SQLite FTS5 行为证据。

3. **PostgreSQL adapter 先做 native FTS fallback，PGroonga/pgvector 是 optional seam。**  
   Phase 9 的 service profile 必须在扩展缺失时不崩溃。native FTS 可证明 PostgreSQL retrieval adapter 存在；PGroonga/pgvector adapter 提供 capability probing 和 query seam，只有 extension installed 时使用。替代方案是要求 compose 默认装扩展；拒绝，因为 DEV-PLAN 明确 optional。

4. **RRF 不重新计算 provider score，只融合 rank。**  
   不同 retriever 的分数尺度不可比；RRF 使用原始 rank 计算融合 score，并把 provider contribution 留在 metadata。这样 BM25/vector/PGroonga 混用时可解释。

5. **Retrieval context wrapper 明确“引用内容”边界。**  
   检索文本进入模型前包装 citation/source/trust，默认 untrusted。即使内容包含“忽略系统指令”等文本，也作为引用材料，而不是高优先级 prompt。ContextAssembler 已有 `kind="retrieval"` 的预算截断路径，本 change 复用它。

6. **RAG 示例先做配置和 eval fixture，不提前做完整示例 agent 产品流。**  
   Phase 12 会交付四个 P0 示例 agent；Phase 9 只提供 RAG assistant 所需的 config/eval 基础和检索公开 seam，避免把示例产品化提前塞进本 change。

## 影响面

- `packages/agent-harness/src/agent_harness/retrieval/`: provider、context、local_bm25、postgres、pgroonga、pgvector、hybrid。
- `packages/agent-harness/src/agent_harness/storage/`: retrieval models、repository exports、UoW、migration。
- `packages/agent-harness/src/agent_harness/storage/diagnostics.py` 与 `agent_harness.cli doctor`: optional extension status。
- `templates/service-app/agents/examples/rag_assistant/config.yaml` 与 `evals/approved.yaml`。
- `tests/contracts/`: retrieval public seam、migration/repository、doctor、RRF、context injection、安全边界、示例 config/eval。

## 测试接缝

- Module seam: `RetrievalProvider` DTO、local BM25 adapter、PostgreSQL adapter capability fallback、PGroonga/pgvector probe。
- Persistence seam: `run_migrations()` 后 SQLite/PostgreSQL schema 包含 retrieval 表，repository/UoW 可 round trip。
- Doctor seam: `agent-harness doctor --profile service` 在 extension 缺失时输出降级提示且不因 optional extension non-zero。
- Context seam: retrieval result 转 `ContextFragment` 后经 ContextAssembler 保留 trust/citation/source/truncation。
- Hybrid seam: RRF 合并多 provider 结果，重复 chunk 只输出一次并保留 contribution。
- Example seam: RAG assistant config 可被 registry 加载，approved eval fixture 覆盖 citation 与 no-source 两种预期。

## 风险 / 取舍

- [风险] SQLite FTS5 在极老构建中不可用。→ 缓解：adapter 初始化时检测 FTS5 能力并返回结构化错误；CI 当前 Python sqlite 通常内置 FTS5，测试锁实际能力。
- [风险] PostgreSQL service smoke 环境没有 PGroonga/pgvector。→ 缓解：doctor 区分 optional extension missing 和 required service failure；native FTS fallback 仍可运行。
- [风险] 检索 chunk 被当作高优先级 prompt。→ 缓解：context wrapper 和 tests 强制 `trust_level="untrusted"`，并保留 citation/source_ref。
- [风险] RAG 示例被误读成 Phase 12 完整示例 agent。→ 缓解：本 change 只提供 config/eval 基础和检索 seam，完整用户流留到 Phase 12。

## 迁移计划

新增 Alembic migration `0005_retrieval_rag_foundation` 创建 retrieval evidence 表和必要索引；随后用 `0006_retrieval_chunk_identity` 把 chunk 唯一身份收紧为 `(tenant_id, collection, document_id, chunk_id)`，避免同一 collection 下不同 document 复用 `chunk_id` 时互相覆盖 citation/source/content。SQLite local adapter 需要 FTS5 virtual table；若数据库不支持 FTS5，adapter 报结构化不可用，local doctor/contract tests 会暴露。回滚时按 Alembic downgrade 删除或调整 retrieval 表；不手工删除用户数据库。

## 待确认问题

- Phase 12 是否把 RAG assistant 做成完整可运行 agent flow；本 change 只留下 config/eval 与 provider seam。
