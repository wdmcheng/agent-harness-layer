## 1. 红灯合同与迁移边界

- [x] 1.1 增加 repository/provider red contracts，复现相同输入跨 tenant 命中与 ref 复用，并逐值断言同 tenant 重放、跨 repository instance 命中、provider 调用次数和持久化 `cache_status`/`vector_ref`/`provider_latency_status`/nullable `provider_latency_ms`；新写入必须 recorded + 非负数值，不能使用 migration 专属 unavailable。
- [x] 1.2 增加 `0012a_embedding_cache_tenant_scope` SQLite migration contracts，覆盖带既有 row upgrade到 `tenant_embedding_cache`、字段/时间戳/原 metadata 键保留、目标约束与两个目标索引名精确、旧 constraint/index 和 `embedding_cache` table/view/alias 不存在，统一 metadata 缺失/相等时增量 backfill、两种 latency key 都缺失时补 unavailable/null 且不猜 `0`、冲突/非法/status-value 不一致时 mutation 前拒绝，以及四列唯一约束；分别模拟旧 binary 查询新 schema 与新 repository 查询旧 schema，证明都在返回 row 前失败且零 mutation；downgrade 验证空库无/非法/重复 opt-in 拒绝、精确 `-x allow_empty_evidence_downgrade=true` 恢复旧表名/三列约束/索引名和有 evidence 即使 opt-in 也 fail closed。
- [x] 1.3 增加真实 PostgreSQL migration/repository contracts，使用隔离数据库验证与 SQLite 相同的物理表切换、双向 application/schema mismatch fail-closed、metadata 四态 preflight/backfill、唯一性、tenant lookup、显式 downgrade opt-in 和 evidence 保留结果。

## 2. Tenant-Scoped Cache 实现

- [x] 2.1 修改 ORM 与 repository，ORM 物理表固定为 `tenant_embedding_cache`，lookup/put/唯一性显式要求 `tenant_id`，内部 put 查重不误记 hit；增加 tenant-scoped DTO/UoW 与旧表名不可访问回归测试。
- [x] 2.2 修改 local 与 OpenAI-compatible providers，使用完整 tenant hash 派生不同 `vector_ref`，统一新 miss metadata 为 `provider_latency_status=recorded` + 非负 `provider_latency_ms`，并在命中时持久化 hit、保留 recorded/unavailable 历史状态且不再次调用 provider。
- [x] 2.3 实现 `0012a` SQLite/PostgreSQL 迁移、legacy metadata 全量预检/增量 backfill、`embedding_cache -> tenant_embedding_cache` 原子物理表切换和旧名称移除，以及同时要求空 evidence 与精确 Alembic `-x allow_empty_evidence_downgrade=true` 的 downgrade，保留既有 row 与原 metadata 键；把后续 trace `0013` 的直接前置改为 `0012a`，验证 Alembic 只有一个线性 head。

## 3. 联合回归与收口

- [x] 3.1 运行 embedding、storage、migration 定向 unit/contract/integration tests，以及真实 PostgreSQL/Redis service smoke 中的 migration/cache 证据，分别记录离线与 service 结果。
- [ ] 3.2 运行 import/secret scans、`make quality`、`make test`、`make build`、`make license-check`、pre-commit、`git diff --check`、本 change 和 `openspec validate --all --strict`；完成同 digest 的 3 个 fresh code-reviewer Stage 1/2 PASS 后只停在 `ready-to-archive`。
