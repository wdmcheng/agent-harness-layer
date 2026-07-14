## Source Links

- Product-Spec.md：6.2“所有核心数据都按租户隔离”、6.3“持久化业务实体直接带 `tenant_id`”以及 REQ-012。
- DEV-PLAN.md：Phase 6 `EmbeddingProvider`/`embedding_cache` 已实现基线、数据库表 `embedding_cache`，以及 Phase 13.6A `0013` 迁移前置关系。
- Design-Brief.md or design artifact：不涉及 UI 或交互设计。
- CONTEXT.md / ADR：ADR-0001 只定义 service 部署边界，本修复不改变该决策；仓库无相关 CONTEXT.md。

## Why

现有 `embedding_cache` 查询键与唯一约束未包含 `tenant_id`，相同 provider/model/input hash 会把一个租户的 cache 记录和 `vector_ref` 返回给另一个租户；同时持久化 metadata 未稳定记录规范要求的 hit/miss、`vector_ref` 与 provider latency。该缺陷违反已确认的租户隔离和长期 embedding cache 契约，必须在 Phase 13.6A 及其后续 usage/delegation 工作前闭环。

## What Changes

- **BREAKING**：把物理表切换为 `tenant_embedding_cache`，移除旧 `embedding_cache` 名称，并把 cache identity 收紧为 `(tenant_id, provider, model, input_hash)`；所有读取、写入、幂等复用和唯一约束都必须按已认证 tenant 隔离，旧 binary 在新 schema 上查询即 fail closed。
- 为不同租户的相同输入生成不同 `vector_ref`，不得以相同 URI/ref 暗中复用跨租户向量。
- 统一 local 与 OpenAI-compatible adapter 的持久化 cache metadata，记录最近一次 cache outcome、`vector_ref` 和首次 provider latency；新写入必须以 `provider_latency_status=recorded` 携带非负数值，旧合同允许但无法确定 latency 的历史 row 以 `provider_latency_status=unavailable` 与 `provider_latency_ms=null` 无损表达；命中不得伪造一次新的 provider 调用或 latency。
- 新增插入式 Alembic revision `0012a_embedding_cache_tenant_scope`，接在 `0012_service_runtime_execution_context` 后；它保留既有 metadata 键并确定性补齐 `cache_status`、`vector_ref`、`provider_latency_status` 与 nullable `provider_latency_ms`，后续 trace revision `0013` 显式依赖本 revision，并由 `0013a_run_trace_event_hardening` 作为线性硬化 head。
- 新旧 application/schema 组合通过物理表名双向不兼容在读取前 fail closed；downgrade 同时要求没有任何 tenant cache evidence，且操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true`，才恢复旧表名与三列约束。参数缺失/重复/非法或存在记录时都在 DDL 前拒绝。

## Non-Goals

- 不实现 Phase 13.6A、13.7、13.8、13.9、14 或 15 的其他能力。
- 不引入向量数据库、跨租户共享 cache、cache eviction、计费、usage event、trace transport 或新的公开 HTTP API。
- 不重写历史 `vector_ref` 列或既有 metadata 键、不删除 cache evidence，也不自动归档任何 OpenSpec change。
- 不把 `openspec validate` 当作语义审查结论。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `agent-registry-model-context`：embedding cache identity 增加 tenant，跨租户记录/ref 隔离，并明确持久化 hit/miss、`vector_ref` 与 provider latency 的语义。
- `storage-migration-uow`：增加 `0012a_embedding_cache_tenant_scope` 的 SQLite/PostgreSQL upgrade、证据保护 downgrade 与后续 `0013` 依赖合同。

## Impact

- 受影响代码：`EmbeddingCacheModel`、`EmbeddingCacheRepository`、local/OpenAI-compatible embedding providers、storage UoW exports；ORM table 改为 `tenant_embedding_cache`。
- 受影响数据：`embedding_cache` 原子切换为 `tenant_embedding_cache`，唯一约束由三列改为四列；既有 row 和 metadata 键保留并增量补齐统一 cache evidence 字段，未来记录不再跨 tenant 复用。
- 受影响迁移：新增 `0012a_embedding_cache_tenant_scope`，`run-trace-correlation` 的 `0013` 改为从该 revision 继续。
- 受影响测试：SQLite/PostgreSQL migration、跨 repository cache hit、跨 tenant 相同输入隔离、持久化 metadata、adapter provider 调用次数和 downgrade fail-closed。
- 不新增公开 API、依赖或 UI surface。
