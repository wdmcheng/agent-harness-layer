## Context

`EmbeddingRequest` 已携带 `tenant_id`，但 `EmbeddingCacheRepository.get()`、`put()` 的既有查重路径和数据库唯一约束只使用 provider/model/input hash。相同输入因此可跨 tenant 返回同一持久化记录；local 与 OpenAI-compatible adapter 的 metadata 字段也不一致，且命中后没有持久化 hit 状态。当前 Alembic head 是 `0012_service_runtime_execution_context`，相关下游 change 已预留 `0013`、`0014`、`0015`。

## Goals / Non-Goals

**Goals:**

- 让 cache 查询、复用、唯一性和 `vector_ref` 全部 tenant-scoped。
- 在同一 cache row 的 metadata 中持久化可验证的最近一次 hit/miss、稳定 `vector_ref`，以及首次真实 provider latency 或历史 unavailable 状态。
- 以 SQLite/PostgreSQL 一致、保留既有数据的迁移切换到 `tenant_embedding_cache` 并修正唯一约束，使新旧 application/schema 组合双向 fail closed，同时保持后续 `0013` 至 `0015` 编号不变。
- 让新代码、schema、迁移与测试在一个聚焦 change 中闭环，再作为 Phase 13.6A 的前置依赖。

**Non-Goals:**

- 不增加跨租户共享策略、cache eviction、命中计数、访问日志、向量存储后端或公开 API。
- 不为命中伪造 provider 调用、latency、token 或 cost evidence。
- 不实现 run trace、model usage、delegation、SSE 或 Phase 14/15。

## Decisions

1. **tenant 是 cache identity 的第一部分，物理表名也切断旧读取。** Repository 的公开 lookup 必须要求 `tenant_id`，ORM/数据库物理表改为 `tenant_embedding_cache`，唯一约束改为 `(tenant_id, provider, model, input_hash)`，升级后不保留 `embedding_cache` table/view/alias。旧 binary 因查询旧表失败而不能恢复跨租户 lookup，新 binary 在旧 schema 上也因新表缺失而在读取前失败。替代方案是继续全局去重并只隐藏 owner或只加新代码 revision check；拒绝，因为 `vector_ref` 本身仍是跨租户 evidence，且旧 binary 不包含新检查。
2. **`vector_ref` 按 tenant 派生但不暴露 tenant 原文。** 新 ref 使用完整 `SHA-256(tenant_id)` 作为 tenant component，再拼 provider/model/input hash。相同 tenant 的重放稳定，不同 tenant 不同；不使用截断 hash，避免人为引入碰撞空间。既有 ref 不重写，因为本 change 不掌握潜在外部向量存储的 rename 能力；它只继续归原 row 的 tenant 使用。
3. **cache lookup 负责持久化最近一次 outcome。** 新 miss 必须写入 `cache_status="miss"`、`vector_ref`、`provider_latency_status="recorded"`、非 bool 非负 `provider_latency_ms` 和既有维度信息；成功 lookup 在返回前把同一 row 的 `cache_status` 改为 `"hit"`，保留原始 `vector_ref` 与首次 provider latency 状态。旧合同允许但两种 latency key 都缺失的历史 row 只在 migration 中补为 `provider_latency_status="unavailable"`、`provider_latency_ms=null`，不能猜测 `0`；后续 hit 继续保留 unavailable，不把它伪装成 provider 调用。这里不增加 hit counter，避免 PostgreSQL/SQLite JSON 原子递增差异；多个并发 hit 写入相同值，结果幂等。Repository 内部 `put()` 查重使用不记录 hit 的私有读取，避免把创建幂等检查误记为消费命中。
4. **插入式 revision 保留已审迁移编号。** 新 revision 为 `0012a_embedding_cache_tenant_scope`，`down_revision="0012_service_runtime_execution_context"`；trace `0013` 明确从 `0012a` 继续，已发布 `0013` 的事件 shape 漂移由线性后继 `0013a_run_trace_event_hardening` 收敛。替代方案是改写已应用 `0013` 或整体重编号；拒绝，因为前者破坏迁移不可变性，后者只增加契约 churn。
5. **upgrade 保留 evidence并原子切换物理表。** Migration 在任何 DDL/UPDATE 前读取并预检全部旧 row；`metadata_json` 必须是 object，number 必须是非 bool 的非负 int/float。每个统一字段按缺失、相等、冲突、非法四态处理：`cache_status` 缺失时取合法 legacy `cache`，两者都缺失时按“旧 row 只由 provider miss 创建”的既有写入事实取 `miss`；两者都存在时必须同为 `miss|hit` 且相等，否则拒绝。metadata `vector_ref` 缺失时取 row 列，存在时必须是与 row 列逐值相等的非空 string，否则拒绝。存在合法 `provider_latency_ms` 或 legacy `latency_ms` 时，两者若同时存在必须数值相等，并补/校验 `provider_latency_status="recorded"`；两种 latency key 都缺失是旧 DTO 允许的状态，migration 保留全部原键并补 `provider_latency_status="unavailable"`、`provider_latency_ms=null`，不得猜测数值。任一 latency 值非法、两键冲突、status 与数值组合不一致时才整批拒绝。只有全量预检通过后才保留所有旧键并补缺失统一键，已有相等键不改写，再把关系切换为 `tenant_embedding_cache` 且移除旧名称。SQLite 使用 Alembic batch recreate/rename，PostgreSQL 在 migration lock 内 rename并 drop/create constraint/index；两者保留 row id、owner、ref、原 metadata 键和时间戳。旧约束/索引名只在 upgrade 前存在，目标名固定为 `uq_tenant_embedding_cache_tenant_provider_model_hash`、`ix_tenant_embedding_cache_tenant_id`、`ix_tenant_embedding_cache_input_hash`。
6. **downgrade 与旧 binary rollback 均由 schema fail closed。** 只要 `tenant_embedding_cache` 有任一 row，就拒绝恢复旧表名和三列唯一约束，因为旧代码会忽略 tenant；即使新表为空，也只有 Alembic x 参数精确为 `allow_empty_evidence_downgrade=true` 时允许回到 `0012` 并恢复 `embedding_cache`。缺失、重复或其他值均在 DDL 前拒绝。部署必须先停 writers并完成 migration/revision 检查再启用新进程；迁移后的旧 binary 查询不存在的 `embedding_cache`，新 binary 连接未迁移 schema 查询不存在的 `tenant_embedding_cache`，两者都在返回 evidence 前失败。存在 cache evidence 后不得执行 schema downgrade；应用回滚无需依赖旧 binary 自觉检查。

## Affected Surfaces

- `storage/models.py`、`storage/repositories.py`、UoW：`tenant_embedding_cache` 物理表、tenant-scoped identity 与持久化 metadata。
- `embeddings/provider.py`、`adapters/models/openai_compatible_embeddings.py`：tenant-scoped lookup/ref 与统一 miss metadata。
- Alembic versions/runner：`0012a` 和后续 `0013` down revision。
- SQLite/PostgreSQL migration contracts、embedding provider/repository contracts、service smoke migration evidence。
- 不影响公开 HTTP/OpenAPI、配置、UI 或依赖。

## Testing Seams

- 使用两个 tenant 对相同 provider/model/input 发起请求，验证各自首次 miss、各自第二次 hit、记录 id/ref 不同且 provider 每 tenant 只调用一次。
- 重建 UoW/repository 后验证同 tenant 命中；直接查询持久化 row 验证 `cache_status`、`vector_ref`、`provider_latency_status` 与 nullable `provider_latency_ms`。
- SQLite 与真实 PostgreSQL 从 `0012` 带既有 cache row 升级，验证 row/原 metadata 键逐值保留、统一 metadata 字段确定性补齐、缺失 latency 无损标记 unavailable、物理表切换、旧名称不存在、新四列唯一约束生效、跨 tenant 同 key 可插入、同 tenant 重复被拒绝；非法或冲突 legacy metadata 必须在任何 mutation 前整批拒绝。
- 旧 binary 查询 `0012a` 的旧表名、新 repository 查询 `0012` 的新表名均在返回 row 前失败且零 mutation；不得用兼容 view/alias 伪造 application revision gate。
- 空库只有显式 `-x allow_empty_evidence_downgrade=true` 才可 `0012a -> 0012`；无 opt-in 的空库和任一有 cache row 的数据库都在 constraint drop 前 fail closed，revision 和 evidence 保持不变。
- 静态/call-site contract 验证所有 cache lookup 显式传 tenant，业务 agent 不绕过 provider/repository seam。

## Risks / Trade-offs

- [Risk] 旧 `vector_ref` 没有 tenant component → 迁移后只允许 owner tenant 读取既有 row；不猜测外部存储 rename，后续 miss 使用新格式。
- [Risk] SQLite batch recreate 或 metadata backfill 可能丢失 evidence → migration contract 逐列、逐索引、逐 row 与逐 metadata 键对比升级前后，外键模式保持开启；所有 metadata 先预检后更新。
- [Risk] 新 schema 配旧 binary 会恢复跨租户查询 → `0012a` 移除旧物理表名，旧 binary 在查询时 fail closed；有 evidence 时禁止 schema downgrade，部署顺序与双向 mismatch contracts 防止混跑。
- [Risk] metadata 保存“最近一次状态”不提供完整访问历史 → 本 change 只满足 cache evidence 合同；调用级 usage/trace 由 Phase 13.7 的 durable evidence 负责，不在 cache row 中复制事件系统。

## Migration Plan

先以 red contracts 复现跨 tenant 命中、metadata 缺口和新旧 application/schema mismatch；停 writers后实现 `0012a` 的 `embedding_cache -> tenant_embedding_cache` 物理切换并验证 SQLite/PostgreSQL upgrade/downgrade，再切换 ORM/repository/provider。`run-trace-correlation` 的 `0013` migration 以 `0012a_embedding_cache_tenant_scope` 为 `down_revision`，`0013a_run_trace_event_hardening` 再从 `0013` 线性前滚。所有定向与全量验证通过、3 个 fresh reviewer 在同一 digest 上 Stage 1/2 PASS 后只停在 `ready-to-archive`，不自动归档。

## Open Questions

无。跨租户共享 cache、向量后端 rename 和访问历史属于 P0 范围外能力。
