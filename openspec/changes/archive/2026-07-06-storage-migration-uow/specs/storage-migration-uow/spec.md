## ADDED Requirements

### Requirement: Async storage migration 创建 P0 核心 schema
`agent_harness` package SHALL 提供 SQLAlchemy 2.0 async typed models 和 Alembic migration，用于创建 P0 所需的租户、session、run、checkpoint、event、trace、artifact、eval、policy 和 audit 核心表。

#### Scenario: Local SQLite migration 创建 schema
- **WHEN** developer 使用 local profile 执行 migration
- **THEN** SQLite database 包含 `tenants`、`sessions`、`agent_runs`、`checkpoints`、`canonical_events`、`trace_refs`、`artifacts`、`eval_cases`、`eval_runs`、`policy_rules` 和 `audit_logs` 表，并记录当前 migration revision

#### Scenario: Service PostgreSQL migration 创建 schema
- **WHEN** developer 使用 service profile 连接 PostgreSQL 执行 migration
- **THEN** PostgreSQL database 包含同一批核心表，并记录当前 migration revision

### Requirement: Repository 和 UnitOfWork 隔离 ORM session
package SHALL 暴露 repository interface 和 UnitOfWork，使 app、API、agent、eval 和 runtime 调用方不直接依赖 SQLAlchemy session。

#### Scenario: Repository contract 在 adapters 间一致
- **WHEN** 同一 repository contract tests 分别运行在 SQLite 和 PostgreSQL adapter 上
- **THEN** tenant、session、run 和 checkpoint 的创建、查询、更新和事务回滚行为一致

#### Scenario: 业务入口不直接持有 ORM session
- **WHEN** static import/session boundary check 扫描 `templates/service-app/app/*`、`templates/service-app/agents/*`、`examples/*` 和 eval 入口
- **THEN** 扫描不到直接创建或传递 SQLAlchemy `Session` / `AsyncSession` 的业务代码

### Requirement: Doctor 报告 storage、migration 和 service dependency 状态
`agent-harness doctor` SHALL 对选定 profile 报告 storage kind、database connectivity、migration revision、Redis connectivity 和 eval directory 状态。

#### Scenario: Local doctor 不需要外部服务
- **WHEN** developer 运行 `agent-harness doctor --profile local`
- **THEN** command 报告 SQLite/local evidence path 状态，并在无 PostgreSQL/Redis 时成功退出

#### Scenario: Service doctor 报告 PostgreSQL 和 Redis 状态
- **WHEN** developer 运行 `agent-harness doctor --profile service`
- **THEN** command 尝试连接 PostgreSQL 和 Redis，报告 migration revision 和 Redis reachability；连接失败时使用结构化诊断 non-zero 退出

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
