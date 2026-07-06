## 来源链接

- Product-Spec.md：`REQ-005` 存储、迁移与事务边界；`AC-010`、`AC-011`、`AC-012`；`DEP-008`、`DEP-009`、`DEP-011`。
- DEV-PLAN.md：`Phase 3: 存储、迁移与事务边界`；核心表矩阵中 `tenants`、`sessions`、`agent_runs`、`checkpoints`。
- 设计稿 / 架构图：`artifacts/pydantic-ai-agent-architecture.drawio` 中 Storage / Runtime / Event spine 边界。
- CONTEXT.md / ADR：当前仓库无。

## 为什么

Phase 3 必须先把可迁移、可事务化、可替换 adapter 的持久化边界立住。没有这一层，后续 event、runtime、checkpoint 和 service profile 会直接散落操作数据库 session，后面补多租户、worker 拆分和 recovery 会变成返工。

## 变更内容

- 新增 SQLAlchemy 2.0 async typed model、Alembic migration env 和初始核心 schema。
- 新增 SQLite local adapter、PostgreSQL service adapter、Repository interface 与 Unit of Work。
- 扩展 doctor，使 local/service profile 能报告 storage、migration、Redis 和本地证据目录状态。
- 新增 repository contract tests，证明 SQLite 与 PostgreSQL adapter 行为一致；PostgreSQL 不可用时必须显式标记 service proof 缺失，不能用 SQLite 替代。
- 新增 service profile compose/migration 命令入口，为 Phase 5 service smoke 留出共享 storage/queue。

## 非目标

- 不实现 CanonicalEvent、artifact store、runtime state machine、checkpoint resume 或 DBOS adapter。
- 不实现 RAG retrieval、policy engine、approval workflow、eval gate 或完整 API/worker 拆分。
- 不修改 `/Volumes/develop/PyCharmProjects/wiki-brain`；如需 PostgreSQL Dockerfile/entrypoint，仅只读参考。

## 能力

### 新增能力

- `storage-migration-uow`：async storage model、migration、repository、Unit of Work、SQLite/PostgreSQL adapter 和 service storage diagnostics。

### 修改能力

- `typed-config`：service/local profile 的 storage/queue 字段被 storage adapter 与 doctor 使用，但不改变原有加载语义。
- `vendor-boundary-doctor`：doctor 增加 storage/migration/Redis/eval directory diagnostics。

## 影响

- 受影响代码：`packages/agent-harness/src/agent_harness/storage/**`、`packages/agent-harness/src/agent_harness/cli.py`、`templates/service-app/**`、`scripts/**`。
- 受影响依赖：SQLAlchemy、Alembic、asyncpg、aiosqlite；service smoke 可能使用 Docker Compose PostgreSQL/Redis。
- 受影响测试：新增 storage migration 和 repository contract tests，覆盖 local SQLite 和 service PostgreSQL。
- 受影响数据：新增核心表初版；无生产数据迁移。
