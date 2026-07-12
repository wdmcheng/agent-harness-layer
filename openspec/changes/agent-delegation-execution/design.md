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

1. **内置 tool/module 调用 application service。** runtime/worker 注册的 `agent.delegate` seam 调用 `DelegationService`；P0 不新增公开 HTTP endpoint。service 依次执行 parent ownership、registry edge、policy、cycle/depth、预算和幂等校验，再调用 runtime。直接扩展 registry 被否决，因为 registry 不拥有 identity、transaction、queue 或 run 生命周期。
2. **显式 key 与规范化请求 hash 绑定。** hash 覆盖 tenant、有效 identity、parent run、source/target agent、child input 与有效预算；同 key 异 hash fail closed。可选 key 或遗漏安全上下文会把不同授权/预算请求错误合并，因此拒绝。
3. **关系和聚合使用持久化 evidence。** delegation record 保存 parent/child、request hash、状态和 refs；聚合从 child 的可信 terminal/model evidence 计算。接受调用方传入 summary 被否决，因为无法证明来源。
4. **local/service 共享状态机。** local 可内联执行 child；service enqueue 稳定 operation，worker 负责执行并回写聚合。另建 service-only 业务逻辑会造成语义漂移。
5. **未知 token 不伪造完整总量。** `DelegationSummary.input_tokens/output_tokens` 记录已知 child token 的和；任一 child 对应 token 为 null 时，summary 保留已知和并把 `budget_status` 固定为 `incomplete`。预算判定不得把未知值当 0，必要时按 fail closed 或 needs-review 处理。
6. **deny 在创建 child 前完成。** policy/edge/tenant/cycle/depth/budget/idempotency 冲突只允许脱敏 audit/policy evidence，不得创建 child、queue message、provider 或业务 event。
7. **RUN-002 与 aggregation 原子切换。** 同一 change 新增 `RunDetailResponse` 的 agent/parent/delegation 字段、route 映射和 drift test；提前声明空字段或另增 delegation HTTP route都会制造不真实 API，因此拒绝。

## Affected Surfaces

- `agent_harness.registry`、runtime、storage models/repositories/UoW、events/observability。
- service app RUN-002 `RunDetailResponse`、app factory/runtime 依赖、内置 `agent.delegate` tool/module seam 和 worker；公开 route 集合不增加 delegation endpoint。
- Alembic migration、新 delegation/aggregation evidence 表或等价受约束记录。

## Testing Seams

- `DelegationService.delegate()` application seam；local inline 与 service queue contract。
- `agent.delegate` tool/module contract、tenant/edge/policy/cycle/depth/budget/idempotency errors，以及 RUN-002 OpenAPI/DTO 精确切换。
- PostgreSQL 并发同 key、Redis delivery/reclaim、parent-child aggregation 和 deny 零副作用。

## Risks / Trade-offs

- [Risk] child 完成但聚合写入失败 → 同一 transaction/outbox 或可重入 aggregation claim，保留 `needs_review`，不得重复执行 child。
- [Risk] 递归导致无限运行 → P0 只允许单层，child request 不暴露 delegation capability。
- [Risk] usage evidence 不完整 → parent 明确标记 incomplete/needs_review，不伪造成本或预算结论。

## Migration Plan

先加可回滚 migration 与 repository，再接 application service、local runtime、service queue/worker，最后注册内置 `agent.delegate` 并原子切换 RUN-002。回滚必须先证明没有活跃 delegation；有记录时保留读取兼容层，不删除 evidence。

## Open Questions

无阻塞问题；多层 graph 与跨租户策略留到 P1。
