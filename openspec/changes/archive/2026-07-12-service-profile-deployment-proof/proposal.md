## Source Links

- Product-Spec.md: SCOPE-003、SCOPE-005、SCOPE-007、TASK-005、REQ-022，以及部署边界图交付物。
- API-Contract.md: 部署边界、runtime worker、future API/worker split 和 RUN-001 service profile 状态语义。
- DEV-PLAN.md: Phase 13 Docker Compose、`make smoke-service`、独立进程执行、拆分顺序与维护者可解释性验收。
- Design-Brief.md or design artifact: `docs/architecture/agent-harness-deployment-boundaries.*` 是本变更必须同步的部署架构产物。
- CONTEXT.md / ADR: 当前无既有 ADR；本变更创建 `docs/adr/0001-p0-service-boundaries.md` 固定 P0 决策。

## Why

核心 queue 与 split runtime 即使合同测试通过，也不能证明复制后的 service-app 能以四个真实服务协作。需要用 Compose 和可重复 smoke 把 PostgreSQL、Redis、API、worker、迁移、queue pickup、DBOS/shared checkpoint/event 与重试幂等串成可操作证据。

## What Changes

- Docker Compose service profile 增加独立 API 与 worker 服务，使用同一 PostgreSQL、Redis、profile、认证和事件配置。
- `make smoke-service` 启动四服务，通过真实 HTTP RUN-001 提交任务，轮询 run/events 证明 worker 执行同一 run。
- smoke 注入重复请求、重复 queue message 或 worker reclaim 场景，证明 run 与 terminal side effect 不重复。
- 增加容器 health/readiness、迁移先行和只作用于模板 compose project 的清理/诊断边界。
- 同步 README、API contract、DEV-PLAN、部署架构图和 P0 service boundary ADR，固定未来拆分顺序与 DTO/CanonicalEvent/trust/audit 约束。

## Non-Goals

- 不把 P0 改造成完整微服务平台，不拆 tool/model/storage/event 服务。
- 不提供 Kubernetes、生产 secrets 管理、横向扩缩、SaaS provider 或 Phase 15 CI/release automation。
- 不补齐 Phase 14 的全部深度文档和维护者指南。

## Capabilities

### New Capabilities

- `service-deployment-boundaries`: 定义四服务 Compose 协作、真实分进程 smoke、当前部署形态和未来拆分顺序。

### Modified Capabilities

- `service-app-shell`: 把 service smoke 从 PostgreSQL/Redis 可达性与直接 worker run，提升为真实 HTTP API 到独立 worker 的 queue/DBOS/checkpoint/event 证明。

## Impact

- 修改 service-app Dockerfile/Compose、Makefile、profile、smoke 脚本和模板操作文档。
- 新增/更新部署架构图、ADR、API contract 和 DEV-PLAN 状态证据。
- 测试增加 workspace 外复制后的四服务 smoke 与静态 Compose/边界合同；不新增产品 HTTP endpoint。
