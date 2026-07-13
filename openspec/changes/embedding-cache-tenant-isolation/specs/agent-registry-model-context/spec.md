## MODIFIED Requirements

### Requirement: EmbeddingProvider 支持 mock/local、OpenAI-compatible adapter 和 cache
系统 SHALL 通过 `EmbeddingProvider` interface 生成 embedding，local tests 默认使用 mock/local provider，并通过 `tenant_embedding_cache` 持久化记录复用重复输入结果。cache key SHALL 包含 `tenant_id`、provider、model 和 input hash；所有 lookup、幂等复用、唯一性与 `vector_ref` MUST 按 tenant 隔离，不同 tenant 不得返回同一 cache record 或 `vector_ref`。cache metadata SHALL 持久化记录最近一次 hit/miss、稳定 `vector_ref` 和 provider latency 状态；新 provider 写入 MUST 使用 `provider_latency_status=recorded` 与非 bool 非负 `provider_latency_ms`，旧合同允许但无法确定 latency 的历史 row MUST 使用 `provider_latency_status=unavailable` 与 `provider_latency_ms=null`，不得猜测为 `0`。cache hit MUST 保留首次 provider latency 状态且不得伪造新的 provider 调用。新 schema MUST 不再暴露旧物理表名 `embedding_cache`，使忽略 tenant 的旧 binary 在查询时 fail closed。

#### Scenario: 同租户重复 embedding 输入命中 cache
- **WHEN** 同一 tenant、provider、model 和 input hash 第二次请求 embedding
- **THEN** cache 返回该 tenant 已有 vector ref 或 embedding result，把持久化 metadata 记录为 hit，并且不再次调用 provider

#### Scenario: 不同租户相同输入相互隔离
- **WHEN** tenant A 与 tenant B 使用相同 provider、model 和 input hash 请求 embedding
- **THEN** 两个 tenant 分别得到自己的 cache record 与不同 `vector_ref`，任一 tenant 都不能读取或复用另一 tenant 的记录

#### Scenario: Embedding cache 记录可跨 repository instance 复用
- **WHEN** 同一 tenant 在同一 SQLite 或 PostgreSQL storage 中重新构造 embedding cache repository
- **THEN** 第二次请求同一 provider、model 和 input hash 仍命中该 tenant 已有 cache record，持久化 metadata 保留 `vector_ref` 与首次 provider latency 状态；历史 unavailable 不得被改写为虚构数值

#### Scenario: OpenAI-compatible adapter 不污染业务边界
- **WHEN** 配置 OpenAI-compatible embedding provider
- **THEN** provider SDK / HTTP 细节只存在于 adapter 层，业务 agent 和 context assembler 只依赖 `EmbeddingProvider`
