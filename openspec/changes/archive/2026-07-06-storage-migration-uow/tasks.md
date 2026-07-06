## 1. OpenSpec 与依赖基线

- [x] 1.1 运行 `openspec validate storage-migration-uow --type change --strict`，确认本 change artifact 可解析。
- [x] 1.2 新增 SQLAlchemy、Alembic、asyncpg、aiosqlite 和必要 Redis client 依赖，保证 `uv sync` 可解析。

## 2. Migration 与模型

- [x] 2.1 新增 storage model 和 Alembic env / `0001_core_schema` migration，覆盖 tenants、sessions、agent_runs、checkpoints。
- [x] 2.2 新增 local SQLite migration 测试，证明 migration 后 schema 和 revision 存在。
- [x] 2.3 新增 service PostgreSQL migration 命令和测试/smoke seam，证明不能用 SQLite 代替 PostgreSQL 证据。

## 3. Repository 与 UoW

- [x] 3.1 实现 repository interface、UnitOfWork、SQLite/PostgreSQL SQLAlchemy adapter。
- [x] 3.2 新增 repository contract tests，覆盖 create/read/update/rollback/idempotent run lookup，在 SQLite 与 PostgreSQL adapter 行为一致。
- [x] 3.3 扩展 import/session boundary check，阻止 app/API/agent/eval 直接持有 SQLAlchemy session。

## 4. Doctor 与 service profile

- [x] 4.1 扩展 `agent-harness doctor`，报告 storage、migration、Redis、provider key 和 eval directory 状态。
- [x] 4.2 新增或更新本项目 `templates/service-app/docker-compose.yml`、Makefile service 命令和 profile 配置，覆盖 PostgreSQL/Redis。
- [x] 4.3 运行 `make quality`、`make test`、`make smoke-local` 和 storage service smoke，并记录证据。

## 5. 验证证据

- [x] 5.1 `make quality`、`make test`、`make smoke-local` 覆盖 local SQLite migration、repository/UoW、doctor 和 boundary scan。
- [x] 5.2 `make smoke-service` 覆盖本项目 PostgreSQL/Redis compose profile、PostgreSQL migration、Redis reachability 和 repository probe。
- [x] 5.3 PostgreSQL contract test 使用 `AGENT_HARNESS_TEST_POSTGRES_DSN` 单独执行，证明 service adapter 不由 SQLite 证据替代。
