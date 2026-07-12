## Source Links

- Product-Spec.md: REQ-007、AC-015、AC-016、RUN-006 之外的 agent 互调 P0 范围
- DEV-PLAN.md: Phase 13.8 `agent-delegation-execution`
- API-Contract.md: DLG-001 内置 `agent.delegate` tool/module seam、`DelegationSummary`、RUN-002 `RunDetailResponse` 与 delegation 错误码
- Design artifact: `docs/architecture/agent-harness-technical-architecture.*`、`docs/architecture/agent-harness-runtime-trust-boundaries.*`

## Why

当前 registry 只校验 delegation edge 并组装调用方提供的摘要，不能执行 child agent 或把 usage、budget、trace 持久化归并到 parent run。Product P0 已明确要求真实受控互调，因此必须在 Phase 14/15 前补齐可审计的执行闭环。

## What Changes

- 实现 DLG-001 内置 `agent.delegate` tool/module seam；P0 不新增公开 delegation HTTP endpoint。
- 在 edge、policy、tenant、cycle、depth、预算和幂等校验通过后才产生 child run 与 queue/executor 副作用。
- 持久化 parent-child 关系，并把 child token、cost、latency、budget impact 与 trace refs 聚合到 parent 公开摘要。
- 显式 idempotency key 绑定 tenant、identity、parent/source/target、child input 与有效预算；同 key 同 hash 重放，同 key 异请求返回 `delegation.idempotency_conflict` 且零业务副作用。
- 与 `model-usage-evidence` 前序 capability 对接，并把 RUN-002 原子切换为包含 parent/delegation aggregation 的 `RunDetailResponse`。
- 未声明 edge、跨租户、权限拒绝、预算拒绝和 child failure 均形成稳定错误或终态证据，不泄漏 provider payload 或 secret。

## Non-Goals

- 不实现通用 DAG/graph 编排、循环 delegation、动态 agent discovery 或跨租户委派。
- 不实现 Phase 14/15 文档、CI/CD、发布或归档动作。
- 不把 registry 的 `DelegationSummary` 构造器继续当作已验证执行证据。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `agent-registry-model-context`: 从 edge-check/调用方摘要接缝扩展为受控真实 delegation 入口。
- `runtime-checkpoint-runs`: 增加 child run 生命周期、parent-child 持久化与幂等执行边界。
- `service-app-shell`: 保持“无公开 delegation HTTP route”，并把 RUN-002 原子切换为 `RunDetailResponse` 与精确 OpenAPI 契约。

## Impact

- 核心：registry、runtime orchestration、storage repository/UoW、CanonicalEvent 与 model usage evidence。
- 模板：service app RUN-002 route/schema、app factory/runtime 装配、内置 tool/module seam 与 local/service queue 路径。
- 数据：需要可迁移的 parent-child delegation 与 aggregation evidence；所有记录必须带 `tenant_id`。
- 测试：unit、contract、integration、真实 PostgreSQL/Redis service smoke 与 OpenAPI drift。
