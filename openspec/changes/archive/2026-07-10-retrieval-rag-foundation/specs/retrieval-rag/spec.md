## ADDED Requirements

### Requirement: RetrievalProvider 提供 provider-neutral 检索边界
系统 SHALL 暴露 `RetrievalProvider` interface 和稳定 DTO，用于 indexing、query、结果排序、citation 和 chunk metadata 传递。调用方 MUST 只依赖 `agent_harness.retrieval` 的 public seam；SQLite、PostgreSQL、PGroonga、pgvector 或其他 provider 的 SQL、extension object、driver row 和原始响应 MUST 留在 adapter 内部。

#### Scenario: Provider interface 返回稳定检索结果
- **WHEN** 调用方通过 `RetrievalProvider.query()` 执行检索
- **THEN** 返回结果包含 `document_id`、`chunk_id`、`content`、`score`、`rank`、`source_ref`、`citation`、`trust_level` 和 provider metadata，不暴露数据库 row 或 provider 原始对象

#### Scenario: Indexing 写入可追踪 chunk metadata
- **WHEN** 调用方通过 `RetrievalProvider.index()` 写入文档和 chunks
- **THEN** provider 记录 tenant、collection、document metadata、chunk metadata、citation、source_ref、trust_level 和 provider-specific refs

### Requirement: Local SQLite FTS5/BM25 adapter 离线可用
系统 SHALL 提供 local SQLite FTS5/BM25 retrieval adapter。local profile MUST NOT 依赖 PostgreSQL、PGroonga、pgvector 或外部搜索服务即可完成 RAG 示例检索。SQLite BM25 排序 MUST 使用 SQLite FTS5 官方语义：`bm25()` 数值越小匹配越好，结果按升序转换成从高到低的稳定 rank。

#### Scenario: Local profile 返回 BM25 结果
- **WHEN** local profile 索引 RAG 示例文档并查询关键词
- **THEN** 系统返回至少一个 BM25 检索结果，包含 citation 和 source_ref，且不需要 PostgreSQL extension

#### Scenario: Local 空结果稳定返回
- **WHEN** local adapter 查询没有匹配的关键词
- **THEN** 系统返回空结果列表和可追踪 provider metadata，不抛出未处理异常

### Requirement: PostgreSQL retrieval adapter 与 optional extensions 可降级
系统 SHALL 提供 PostgreSQL lexical retrieval adapter，并提供 PGroonga 与 pgvector optional adapter 探测。PGroonga 或 pgvector 未安装时，`agent-harness doctor --profile service` MUST 输出降级提示并保持结构化诊断；系统 MUST NOT 因 optional extension 缺失而启动崩溃。

#### Scenario: Service doctor 报告 optional extension 缺失
- **WHEN** service profile 可连接 PostgreSQL 但未安装 PGroonga 或 pgvector
- **THEN** doctor 输出每个 extension 的 `missing` / `available` / `installed` 状态和降级提示，不把 optional extension 缺失当作服务不可启动错误

#### Scenario: PostgreSQL native FTS 可作为 fallback
- **WHEN** PGroonga 未安装但 PostgreSQL storage 可用
- **THEN** PostgreSQL retrieval adapter 使用 native FTS 查询路径返回 lexical results 或空结果，而不是要求 PGroonga

### Requirement: Hybrid retrieval 使用 RRF 输出可解释 ranking
系统 SHALL 提供 hybrid retrieval + Reciprocal Rank Fusion interface，用于合并 BM25、native FTS、PGroonga、pgvector 或其他 retriever 的结果。RRF 输出 MUST 保留每条候选来自哪些 provider、原始 rank、原始 score、融合 score 和最终 rank。

#### Scenario: RRF 合并 BM25 和 vector 结果
- **WHEN** 输入 BM25 和 vector 两组带 rank 的结果
- **THEN** hybrid adapter 按 RRF 生成去重后的结果列表，并输出每个 chunk 的 provider contribution metadata

#### Scenario: 重复 chunk 只输出一次
- **WHEN** 同一 chunk 同时出现在多个 retriever 结果中
- **THEN** hybrid adapter 只输出一个最终结果，并在 metadata 中保留所有 provider contribution

### Requirement: Retrieval chunk 注入上下文前保留信任边界
系统 SHALL 提供 retrieval context DTO，把检索结果转换为 `ContextFragment(kind="retrieval")`。每个 fragment MUST 保留 citation、source_ref、trust_level、token estimate、truncation metadata 和 optional artifact_ref。检索内容默认 `trust_level="untrusted"`，prompt injection 文本只能作为引用内容进入上下文，不得覆盖 system、policy 或 developer 指令。

#### Scenario: 检索 chunk 转换为 untrusted ContextFragment
- **WHEN** RAG 示例把 retrieval result 注入 ContextAssembler
- **THEN** fragment 的 kind 为 `retrieval`，trust_level 为 `untrusted`，source_ref 和 citation 可从 fragment metadata 或 content wrapper 中追踪

#### Scenario: Prompt injection 文本不能覆盖高优先级指令
- **WHEN** retrieval chunk 内容包含 system override、policy bypass 或 developer instruction 类文本
- **THEN** context DTO 保留原文来源和 untrusted 标记，ContextAssembler 只把它作为引用内容处理，并在 trace 中保留 source_ref/trust/truncation 证据

### Requirement: Retrieval evidence 可持久化
系统 SHALL 持久化 `retrieval_documents` 与 `retrieval_chunks` evidence。Document 记录 SHALL 包含 tenant、collection、document_id、source_ref、citation 和 metadata；Chunk 记录 SHALL 包含 tenant、document_id、chunk_id、content_ref 或 inline content、source_ref、citation、trust_level、rank metadata、vector_ref 和 provider metadata。完整大文本可通过 artifact/ref 保存，repository 对外只暴露 DTO。

#### Scenario: Local migration 创建 retrieval 表
- **WHEN** developer 使用 local profile 执行 migration
- **THEN** SQLite schema 包含 `retrieval_documents` 和 `retrieval_chunks` 表，repository 可写入并读取 document/chunk evidence

#### Scenario: Service migration 创建同形表
- **WHEN** developer 使用 service profile 连接 PostgreSQL 执行 migration
- **THEN** PostgreSQL schema 包含同一批 retrieval evidence 表，并保持与 SQLite repository contract 一致

### Requirement: RAG assistant 示例验证 citation 与未找到出处
系统 SHALL 提供 RAG assistant 示例 config 和 approved eval 基础数据。示例 MUST 能在 fake/local profile 下运行检索验证：有命中时回答带 citation；无命中时明确说明未找到出处。示例 agent 不得直接 import provider SDK 或数据库 driver。

#### Scenario: RAG 示例命中时带 citation
- **WHEN** RAG assistant 示例查询已索引文档中的内容
- **THEN** 示例输出包含 citation 或 source_ref，可追踪到 retrieval chunk

#### Scenario: RAG 示例无命中时说明未找到出处
- **WHEN** RAG assistant 示例查询未覆盖内容
- **THEN** 示例输出明确说明未找到出处，而不是编造引用

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
