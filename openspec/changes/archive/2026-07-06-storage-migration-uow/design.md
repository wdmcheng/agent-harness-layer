## Context

当前包只有 typed config、identity 和公共 DTO。`templates/service-app/configs/profiles/local.yaml` 仍声明 filesystem storage，`service.yaml` 声明 PostgreSQL/Redis，但没有实际连接、迁移或 repository seam。Phase 3 需要建立后续 event/runtime 复用的最小持久化骨架。

## Goals / Non-Goals

**Goals:**
- 用 SQLAlchemy 2.0 async ORM 定义 P0 核心表，并通过 Alembic 初始 migration 创建 schema。
- 暴露 repository interface 和 UnitOfWork，禁止 app/API/agent/eval 直接持有 SQLAlchemy session。
- 同一批 repository contract tests 可在 SQLite 与 PostgreSQL adapter 上运行。
- doctor 能报告 storage/migration/Redis/eval directory 状态。

**Non-Goals:**
- 不在本 change 实现 runtime orchestration、event bus 或完整 service worker。
- 不把 DBOS、provider SDK 或业务 agent 逻辑放进 storage 层。

## Decisions

- SQLAlchemy async engine + `async_sessionmaker` 是唯一 ORM runtime seam。原因：官方 SQLAlchemy 2.0 asyncio 文档支持 `create_async_engine`、`AsyncAttrs`、`DeclarativeBase` 和 `async_sessionmaker`，足够同时覆盖 SQLite 与 PostgreSQL。
- Alembic env 使用 async engine，并在 programmatic migration 时通过 shared connection 执行。原因：Alembic cookbook 推荐 `async_engine_from_config` 和 `connection.run_sync`，能同时支持 CLI 与测试内迁移。
- 模型集中定义在 storage 包，业务层只依赖 repository/UoW。原因：Phase 5 需要 runtime/checkpoint 复用事务边界，不能把 ORM session 泄漏给 API/agent。
- PostgreSQL service proof 必须真实连接 PostgreSQL。原因：SQLite 不支持证明 PostgreSQL migration、driver、network 和 schema 差异。

## Affected Surfaces

- `agent_harness.storage` 新增 models、repositories、uow、SQLAlchemy adapter、migrations。
- `agent_harness.cli` doctor 增加 storage/migration/Redis/eval directory diagnostics。
- `templates/service-app` 增加 compose、Makefile service commands 和 profiles 对齐。
- `scripts` 增加 migration/service smoke helper。

## Testing Seams

- Public module seam：`agent_harness.storage` repository 和 UnitOfWork。
- CLI seam：`agent-harness doctor --profile local/service`。
- Migration seam：local SQLite migration 和 service PostgreSQL migration 命令。
- Contract seam：同一 repository contract test matrix 对 SQLite/PostgreSQL 执行。

## Risks / Trade-offs

- [Risk] 本地环境没有 Docker 或 PostgreSQL → Mitigation：测试层区分 SQLite 必跑与 PostgreSQL service smoke；最终完成前必须给出真实 PostgreSQL 证据。
- [Risk] SQLite/PostgreSQL dialect 差异掩盖问题 → Mitigation：contract tests 不使用 SQLite 结果证明 PostgreSQL，通过 service profile 单独迁移和 smoke。
- [Risk] UoW 抽象过重 → Mitigation：只暴露当前 Phase 需要的 tenant/session/run/checkpoint 最小方法。

## Migration Plan

新建初始 schema migration，不处理存量数据。回滚策略是 drop Phase 3 新表；当前项目无生产库。service profile 使用独立 compose project 和命名 volume，避免污染其他项目。

## Open Questions

- 无。Phase 5 会继续消费本 change 暴露的 run/checkpoint repository seam。
