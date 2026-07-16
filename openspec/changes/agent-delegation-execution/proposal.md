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
- 对同一 parent 的所有并发请求执行原子预算预留；不同 idempotency key 也必须竞争同一余额，未知 usage 保持占用并进入复核，不能按 0 释放。
- 显式 idempotency key 绑定 tenant、identity、parent/source/target、child input 与稳定预算意图；P0 无显式预算参数时使用 `inherit_parent`，动态 parent 余额和锁内计算的有效预留额不进入 hash。新 claim 与首次 parent reservation 同事务提交；同 key 同 hash 即使其他 key 改变余额也复用原 reservation/operation，同 key 异请求在预算写入前返回 `delegation.idempotency_conflict` 且零业务副作用。
- 与 `model-usage-evidence` 前序 capability 对接，并把 RUN-002 原子切换为包含 parent/delegation aggregation 的 `RunDetailResponse`。
- RUN-002 以 durable parent-child relation 决定 summary membership；活动或已终态但未结算的 child 不得因 aggregate row 暂缺而消失，完成与未结算 child 并存时全部返回且预算保持 incomplete，只有确无 child relation 时 summary 才为 null。
- `DelegationService` 必须在 child、queue、provider 或业务事件副作用前消费 `0014` 的受信 event capacity reservation；预约数只由封闭 `operation_kind` registry 派生，容量不足或结果未知时沿用前序稳定恢复语义。
- 固定获准 delegation 的 CanonicalEvent 生命周期：在 parent run 上最多发布 claimed -> child.created -> completed|failed 三条 internal non-terminal event，使用稳定 event id、受控阶段 payload 与可重放顺序；同时把长期 event catalog 校正为与 39 种代码枚举精确相等，并让 terminal type/flag/visibility 双向 fail closed。
- 未声明 edge、跨租户、权限拒绝、预算拒绝和 child failure 均形成稳定错误或终态证据，不泄漏 provider payload 或 secret。
- `0015` downgrade 同时要求 delegation/reservation/aggregation evidence 全空和显式 Alembic `-x allow_empty_evidence_downgrade=true`；任一条件不满足都在 DDL 前拒绝。

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
- `canonical-events-artifacts`: 让真实 delegation 调用点在副作用前消费 `0014` event capacity reservation；固定 delegation 生命周期、39 种 event catalog 与 terminal 双向不变量，并保持 local/PostgreSQL 相同的容量不足、未知结果和零副作用拒绝语义。
- `service-app-shell`: 保持“无公开 delegation HTTP route”，并把 RUN-002 原子切换为 `RunDetailResponse` 与精确 OpenAPI 契约。
- `storage-migration-uow`: 固定 `0015` delegation/reservation/aggregation upgrade 与 evidence-aware downgrade 的 SQLite/PostgreSQL 一致性。

## Impact

- 核心：registry、runtime orchestration、storage repository/UoW、CanonicalEvent、event capacity reservation 与 model usage evidence。
- 模板：service app RUN-002 route/schema、app factory/runtime 装配、内置 tool/module seam 与 local/service queue 路径。
- 数据：在前序 `0014` evidence outbox 后以 `0015` 增加可迁移的 parent-child delegation、budget reservation 与 aggregation evidence；所有记录必须带 `tenant_id`。
- 测试：unit、contract、integration、真实 PostgreSQL/Redis service smoke、OpenAPI drift、精确 event catalog、delegation 生命周期/重放/visibility 与 terminal type/flag/visibility 双向拒绝。
