## Context

registry 当前只提供 edge check 和 `DelegationSummary` DTO，runtime 没有从 parent run 创建 child run 的受控入口，也没有 durable parent-child aggregation。local profile 可同步执行，service profile 必须通过现有 Redis Streams/worker 边界完成；两条路径必须共享同一 application service 与 repository contract。

## Goals / Non-Goals

**Goals:**
- 提供租户隔离、policy、edge、cycle/depth、预算与幂等约束下的单层 delegation。
- 让 parent/child 关系和 usage/budget/trace 聚合成为持久化事实，而不是调用方自报 DTO。
- 保持 local 与 service 路径结果一致，并让拒绝路径零业务副作用。

**Non-Goals:**
- 通用 graph、循环/递归 delegation、跨租户委派和动态发现。
- Phase 14/15、发布或 OpenSpec archive。

## Decisions

1. **内置 tool/module 调用 application service。** runtime/worker 注册的 `agent.delegate` seam 调用 `DelegationService`；P0 不新增公开 HTTP endpoint。service 先完成不创建 delegation/预算/child 业务状态的 parent ownership、registry edge、policy、tenant、cycle/depth 校验，再进入统一 claim/reservation 事务，最后才调用 runtime；拒绝路径仍可写一次允许的脱敏 policy/audit evidence。直接扩展 registry 被否决，因为 registry 不拥有 identity、transaction、queue 或 run 生命周期。
2. **显式 key、稳定请求 hash 与预算 reservation 原子协调。** hash 覆盖 tenant、有效 identity、parent run、source/target agent、child input 与稳定预算意图；P0 request 没有显式预算参数时使用字面量 `inherit_parent`。动态 parent 剩余额度、当前锁内计算的有效预留额和其他可变余额投影不进入 hash。无状态授权通过后，事务先按唯一 `(tenant_id,parent_run_id,idempotency_key)` 读取或创建 claim：既有同 hash 重放或恢复首次持久化的 operation/reservation，不重算余额；既有异 hash 在预算写入前 fail closed；只有新 claim 才在 parent lock/CAS 内按当时剩余额度和当前 policy/descriptor ceiling 计算最坏情况有效预留额，并与 reservation 同一事务提交或回滚。可选 key、遗漏安全上下文、用动态余额计算 hash 或把 claim/reservation 拆成两个事务都会造成错误冲突、重复占用或余额竞态，因此拒绝。
3. **关系和聚合使用持久化 evidence。** delegation record 保存 parent/child、request hash、状态和 refs；聚合从 child 的可信 terminal/model evidence 计算。接受调用方传入 summary 被否决，因为无法证明来源。
4. **预算按 parent 原子预留，不按 idempotency key 各自判断。** 只有全新 idempotency claim 才在同一事务中使用 parent-level row lock 或等价 CAS，把本请求的最坏情况有效预算写入 `delegation_budget_reservations`；不同 idempotency key 也竞争同一 parent 可用余额。reservation 状态至少区分 `reserved`、`settled`、`released`、`needs_review`：child 尚未创建的确定性失败可原子释放，child 已创建后只能用可信 usage 结算；结果未知时继续占用并进入 `needs_review`，不得把未知值按 0 释放。若预留失败，claim 与 reservation 一并回滚并返回 `delegation.budget_exceeded`，零 child/queue/provider/业务事件副作用。
5. **local/service 共享状态机。** local 可内联执行 child；service enqueue 稳定 operation，worker 负责执行并回写聚合。另建 service-only 业务逻辑会造成语义漂移。
6. **未知或非法 token/cost/latency 不伪造完整总量。** `DelegationSummary.input_tokens/output_tokens` 在混合已知/未知 child 时记录已知值之和，在全部 child 对应 token 都为 null 时写 null；任一 child token 为 null 都把 `budget_status` 固定为 `incomplete`。`cost_usd` 只在所有 child cost 可用时求和，任一 unavailable 时为 null；`latency_ms` 只在所有 child latency 已知时求和，任一未知时为 null；两类缺失都强制 `budget_status=incomplete`。bool、负数、NaN/Infinity 或 cost-status 组合不一致不是“未知”，必须在求和/结算前 fail closed 并保持 reservation/needs_review，parent 已用预算不减少、可用余额不增加。预算判定不得把未知值当 0或让非法负值反向冲减。
7. **deny 在创建 child 前完成。** policy/edge/tenant/cycle/depth/budget/idempotency 冲突只允许脱敏 audit/policy evidence，不得创建 child、queue message、provider 或业务 event。
8. **RUN-002 与 aggregation 原子切换。** 同一 change 新增 `RunDetailResponse` 的 agent/parent/delegation 字段、route 映射和 drift test；提前声明空字段或另增 delegation HTTP route都会制造不真实 API，因此拒绝。

## Affected Surfaces

- `agent_harness.registry`、runtime、storage models/repositories/UoW、events/observability。
- service app RUN-002 `RunDetailResponse`、app factory/runtime 依赖、内置 `agent.delegate` tool/module seam 和 worker；公开 route 集合不增加 delegation endpoint。
- Alembic migration、新 delegation/aggregation/budget reservation evidence 表或等价受约束记录。

## Testing Seams

- `DelegationService.delegate()` application seam；local inline 与 service queue contract。
- `agent.delegate` tool/module contract、tenant/edge/policy/cycle/depth/budget/idempotency errors，以及 RUN-002 OpenAPI/DTO 精确切换。
- SQLite/真实 PostgreSQL 对同一 parent 的同 key 并发 claim、claim 后崩溃重试、其他 key 改变余额后重试原 key 仍复用首次 reservation、不同 key 并发预算预留，及 Redis delivery/reclaim、parent-child aggregation 和 deny 零副作用。

## Risks / Trade-offs

- [Risk] child 完成但聚合写入失败 → 同一 transaction/outbox 或可重入 aggregation claim，保留 `needs_review`，不得重复执行 child。
- [Risk] 递归导致无限运行 → P0 只允许单层，child request 不暴露 delegation capability。
- [Risk] usage evidence 不完整 → parent 明确标记 incomplete/needs_review，不伪造成本或预算结论。
- [Risk] 不同 idempotency key 并发读取同一剩余预算会共同放行 → parent-level lock/CAS 原子预留最坏情况预算；未知结果保持占用，可信 usage 到达后再结算。

## Migration Plan

在 trace migration `0013` 和 usage evidence outbox `0014` 之后以 `0015` 增加 `agent_delegations`、`delegation_budget_reservations` 与 `delegation_aggregates`（或经契约证明等价的受约束记录）及 repository，再接 application service、local runtime、service queue/worker，最后注册内置 `agent.delegate` 并原子切换 RUN-002。downgrade 同时要求 delegation/reservation/aggregation evidence 全空和 Alembic x 参数精确为 `allow_empty_evidence_downgrade=true`；缺失、重复、非法 opt-in 或任一 evidence 都在 DDL 前拒绝，保留读取兼容层且不删除 evidence。

## Open Questions

无阻塞问题；多层 graph 与跨租户策略留到 P1。
