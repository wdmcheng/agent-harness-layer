## ADDED Requirements

### Requirement: Embedding cache tenant scope 迁移保留 evidence 并阻止不安全降级
Alembic revision `0012a_embedding_cache_tenant_scope` SHALL 从 `0012_service_runtime_execution_context` 升级，把旧 `embedding_cache` 物理表切换为 `tenant_embedding_cache`，并把唯一约束收紧为 `(tenant_id, provider, model, input_hash)`；目标约束/索引名固定为 `uq_tenant_embedding_cache_tenant_provider_model_hash`、`ix_tenant_embedding_cache_tenant_id`、`ix_tenant_embedding_cache_input_hash`。升级后数据库中 MUST 不存在可供旧 binary 查询的 `embedding_cache` table/view/alias。SQLite 与 PostgreSQL MUST 保留既有 row 的 id、tenant、provider、model、input hash、`vector_ref`、原 metadata 键与时间戳。Migration MUST 在任何 DDL/UPDATE 前完整预检 legacy metadata，然后确定性增量写入 `cache_status`、`vector_ref`、`provider_latency_status` 与 nullable `provider_latency_ms`。统一键缺失时从合法 legacy 键/row 列派生，已存在时必须类型合法且与派生值相等；存在合法 `provider_latency_ms` 或 legacy `latency_ms` 时，status MUST 为 `recorded` 且两键并存时数值相等。两种 latency key 都缺失是旧合同允许的状态，migration MUST 保留全部原 metadata 键并补 `provider_latency_status=unavailable`、`provider_latency_ms=null`，MUST NOT 猜测为 `0`。统一键与 legacy 键冲突、值非法、`vector_ref` 与 row 列不等，或 latency status/value 组合不一致时 MUST 整批 fail closed，不覆盖、猜值或部分提交。`cache_status` 与 legacy `cache` 都缺失时按旧 row 只由 provider miss 创建的合同派生 `miss`。后续 trace revision `0013` MUST 以 `0012a_embedding_cache_tenant_scope` 为直接前置。只要存在任一 tenant cache evidence，downgrade MUST fail closed，且不得删除、改写或暴露该 evidence；旧 binary 因旧物理表不存在而在读取前失败，新 binary 在未升级的 `0012` schema 上因新物理表不存在而失败。即使新表为空，downgrade 也只有在操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true` 时才能恢复名为 `embedding_cache` 的 `0012` 旧表、旧三列约束和旧索引名；参数缺失、重复、值不是精确小写 `true` 或存在任一 evidence 都必须在 DDL 前拒绝。

#### Scenario: SQLite 和 PostgreSQL 升级保留既有 cache evidence
- **WHEN** 操作者把含既有 embedding cache row 的 SQLite 或 PostgreSQL 从 `0012` 升级到 `0012a`
- **THEN** 所有既有 row 字段和原 metadata 键逐值保持不变，统一 metadata 字段被补齐，`tenant_embedding_cache` 四列唯一约束生效，旧物理表名不存在，不同 tenant 可保存相同 provider/model/input hash，同 tenant 重复 identity 被拒绝

#### Scenario: 非法 legacy metadata 在 mutation 前阻止升级
- **WHEN** 任一 legacy row 的 metadata 不是 object、统一键类型非法、统一键与 legacy/row 派生值冲突，provider latency 值是 bool/负数/非有限值，或 latency status/value 组合不一致
- **THEN** SQLite/PostgreSQL migration 在任何 constraint、row 或 revision mutation 前整批失败，错误不回显 metadata 内容，全部 evidence 保持原样

#### Scenario: 历史 latency 缺失可无损升级
- **WHEN** `0012` 合法 cache row 的 metadata 同时缺少 `provider_latency_ms` 与 legacy `latency_ms`
- **THEN** migration 保留该 row、时间戳与全部原 metadata 键，补 `provider_latency_status=unavailable` 和 `provider_latency_ms=null` 后继续原子升级；不得猜测 `0` 或因旧合同允许的缺失阻断整库

#### Scenario: 新 cache 写入必须记录真实 latency 状态
- **WHEN** `0012a` repository/provider 尝试写入新的 cache miss，但缺少 `provider_latency_status=recorded` 或 `provider_latency_ms` 不是非 bool 非负 number
- **THEN** repository 在持久化前拒绝且不创建 cache row；历史 migration 专用的 unavailable 状态不能被新写入复用

#### Scenario: 缺失或相等的统一 metadata 可安全补齐
- **WHEN** 统一键缺失但 legacy/row 派生值合法，或统一键已经存在且与派生值逐值相等
- **THEN** migration 只补缺失键，保留全部原键和值；已有相等键不改写

#### Scenario: 显式确认的空数据库允许降级
- **WHEN** `tenant_embedding_cache` 为空且操作者以 `-x allow_empty_evidence_downgrade=true` 执行 `0012a -> 0012` downgrade
- **THEN** migration 恢复名为 `embedding_cache` 的旧表与三列约束并记录 `0012_service_runtime_execution_context` revision，不删除任何业务 evidence

#### Scenario: 空数据库但没有显式确认仍拒绝
- **WHEN** `tenant_embedding_cache` 为空，但 Alembic x 参数缺失、重复或不是精确 `allow_empty_evidence_downgrade=true`
- **THEN** migration 在任何 constraint drop 前以脱敏错误拒绝，revision 和 schema 保持 `0012a`

#### Scenario: 存在 cache evidence 时降级 fail closed
- **WHEN** SQLite 或 PostgreSQL 的 `tenant_embedding_cache` 存在任一 row，操作者尝试 `0012a -> 0012` downgrade
- **THEN** migration 在任何 constraint drop 或 row mutation 前以脱敏错误拒绝，revision 与全部 cache evidence 保持不变

#### Scenario: 后续 trace migration 保持单一线性 head
- **WHEN** 数据库从当前 head 继续升级 Phase 13.6A trace revision `0013`
- **THEN** Alembic revision 链严格为 `0012_service_runtime_execution_context -> 0012a_embedding_cache_tenant_scope -> 0013_run_trace_correlation -> 0013a_run_trace_event_hardening`，不存在并行 head、改写已应用 revision 或跳过 tenant 修复的路径

#### Scenario: 新旧 application/schema 组合双向 fail closed
- **WHEN** 旧 binary 在 `0012a` schema 查询 `embedding_cache`，或新 binary/repository 在未升级的 `0012` schema 查询 `tenant_embedding_cache`
- **THEN** 数据库在返回任一 cache row 前以缺失关系失败，不存在兼容 table/view/alias；contract 必须分别断言零跨租户结果和零 cache mutation
