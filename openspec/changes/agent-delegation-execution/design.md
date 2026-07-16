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
3. **关系和聚合使用持久化 evidence，RUN-002 先投影关系再叠加结算。** delegation record 保存 parent/child、request hash、状态和 refs；RUN-002 以带 `child_run_id` 的 durable relation 决定 `children` membership，并从 child run 读取持久化 `RunStatus`。terminal/model aggregate 只补充已结算 usage/cost/latency，不得决定 child 是否存在。活动 child 或已终态但 aggregation 尚未写入的 child 以 unknown 数值进入 summary 并强制 incomplete；已结算与未结算 child 并存时全部返回、只累计已知 token，只有确无 child relation 时 summary 才为 null。接受调用方传入 summary 或按 aggregate row 过滤 child 被否决，因为前者无法证明来源，后者会在恢复窗口漏掉 durable child。
4. **预算按 parent 原子预留，不按 idempotency key 各自判断。** 只有全新 idempotency claim 才在同一事务中使用 parent-level row lock 或等价 CAS，把本请求的最坏情况有效预算写入 `delegation_budget_reservations`；不同 idempotency key 也竞争同一 parent 可用余额。reservation 状态至少区分 `reserved`、`settled`、`released`、`needs_review`：child 尚未创建的确定性失败可原子释放，child 已创建后只能用可信 usage 结算；结果未知时继续占用并进入 `needs_review`，不得把未知值按 0 释放。若预留失败，claim 与 reservation 一并回滚并返回 `delegation.budget_exceeded`，零 child/queue/provider/业务事件副作用。
5. **预算预约与 event capacity 预约共同守住副作用边界。** 通过无状态授权后，全新 delegation claim 必须在创建 child、投递 queue、调用 provider 或发布业务 event 前，通过 `0014` 的受信、版本化、封闭 registry 以 `operation_kind=delegation` 派生最大 prerequisite event 数；调用方不得提供或缩小预约数。claim、parent budget reservation 与 event capacity operation/reservation 必须在同一 application UoW 内提交或回滚；同 key/hash 重放复用首次持久化 operation，不再次预约。容量不足以内部稳定错误 `event.sequence_exhausted` 在副作用前拒绝，且不得留下 child、queue、provider call 或业务 event；结果未知时 event capacity reservation 与 budget reservation 都保持占用并阻止 parent terminal，不能按零释放。
6. **local/service 共享状态机。** local 可内联执行 child；service enqueue 稳定 operation，worker 负责执行并回写聚合。两条路径必须调用同一 `DelegationService` event capacity seam；另建 service-only 业务逻辑会造成语义漂移。
7. **未知或非法 token/cost/latency 不伪造完整总量。** `DelegationSummary.input_tokens/output_tokens` 在混合已知/未知 child 时记录已知值之和，在全部 child 对应 token 都为 null 时写 null；任一 child token 为 null 都把 `budget_status` 固定为 `incomplete`。`cost_usd` 只在所有 child cost 可用时求和，任一 unavailable 时为 null；`latency_ms` 只在所有 child latency 已知时求和，任一未知时为 null；两类缺失都强制 `budget_status=incomplete`。bool、负数、NaN/Infinity 或 cost-status 组合不一致不是“未知”，必须在求和/结算前 fail closed 并保持 reservation/needs_review，parent 已用预算不减少、可用余额不增加。预算判定不得把未知值当 0或让非法负值反向冲减。
8. **deny 在创建 child 前完成。** policy/edge/tenant/cycle/depth/budget/idempotency 冲突只允许脱敏 audit/policy evidence，不得创建 child、queue message、provider 或业务 event。
9. **RUN-002 与 relation-first aggregation 原子切换。** 同一 change 新增 `RunDetailResponse` 的 agent/parent/delegation 字段、route 映射和 drift test；读取 seam 在同一一致性边界联合 relation、child run、reservation 与已存在 aggregate。仅活动 child、已终态但未聚合 child、已结算与未结算 child 并存、确无 child 四类场景分别验收 membership、status、null 与 incomplete 语义。提前声明空字段、按 aggregate row 判断 child 存在、或另增 delegation HTTP route 都会制造不真实 API，因此拒绝。
10. **Delegation 生命周期复用 CanonicalEvent，不建立第二套审计流。** 获准 delegation 在 parent run 上最多发布 claimed -> child.created -> completed|failed 三条事件；final 互斥，pre-child 确定性失败为 claimed -> failed，unknown/needs_review 不发布 final。四种类型固定为 run-scoped、internal、non-terminal，继承 parent canonical trace 与 source agent，event id 分别以 `claimed`、`child`、`final` 稳定收敛。公共 payload 只包含 delegation/source/target；child.created status 封闭为 `queued|running|completed|failed`；final summary 必须严格符合 API Contract 5.30 `DelegationSummary`，child identity 只由 `summary.children` 表达，final 不增加顶层 `child_run_id`；阶段字段除此之外只允许 status 与稳定 error_code。拒绝 child input、完整 identity/hash、动态余额、原始 usage、resume token、secret、路径和原始异常。固定 event catalog 必须与 39 种代码枚举精确相等；`terminal=true` 当且仅当三种 run terminal type，三种必须 public，其他类型必须 non-terminal。另建 delegation event store、公开别名或单向只校验 terminal visibility 都会造成恢复漂移或让 SSE 提前关闭，因此拒绝。

## Affected Surfaces

- `agent_harness.registry`、runtime、storage models/repositories/UoW、events/observability。
- service app RUN-002 `RunDetailResponse`、app factory/runtime 依赖、内置 `agent.delegate` tool/module seam 和 worker；公开 route 集合不增加 delegation endpoint。
- Alembic migration、新 delegation/aggregation/budget reservation evidence 表或等价受约束记录。

## Testing Seams

- `DelegationService.delegate()` application seam；local inline 与 service queue contract。
- `0014` event capacity repository seam；验证全新 claim 在副作用前以封闭 `operation_kind=delegation` 预约、同 key/hash 重放不重复预约、容量不足零 child/queue/provider/业务 event，以及未知结果保持预约并阻止 terminal。
- `agent.delegate` tool/module contract、tenant/edge/policy/cycle/depth/budget/idempotency errors，以及 RUN-002 OpenAPI/DTO 精确切换；真实 repository-backed reader 覆盖仅活动 child、已终态但未聚合 child、已结算与未结算 child 并存、确无 child 四类 relation-first 投影。
- CanonicalEvent contract 精确比较 39 种固定目录；EventBus 与 local/PostgreSQL sink 直接反例覆盖 terminal type/flag/visibility 双向拒绝，并证明拒绝不分配 seq、不消费容量、不物化 artifact、不 fan-out。
- Delegation lifecycle contract 覆盖 claimed/child/final 顺序和稳定 event id、completed/failed 互斥、pre-child failed、needs_review 无 final、parent run/trace/source agent、internal 默认过滤、授权 internal 读取、payload allowlist，以及 local/service/worker reclaim 重放不增加事件数。
- SQLite/真实 PostgreSQL 对同一 parent 的同 key 并发 claim、claim 后崩溃重试、其他 key 改变余额后重试原 key 仍复用首次 reservation、不同 key 并发预算预留，及 Redis delivery/reclaim、parent-child aggregation 和 deny 零副作用。

## Risks / Trade-offs

- [Risk] child 完成但聚合写入失败 → 同一 transaction/outbox 或可重入 aggregation claim，保留 `needs_review`，不得重复执行 child。
- [Risk] child relation 已提交但 aggregate 尚未生成，RUN-002 把它误报为不存在 → membership 只由 durable relation 决定，child run 提供状态，reservation/aggregate 只决定数值完整性；活动与未结算 child 强制 incomplete，并以混合场景防止后续回归。
- [Risk] 递归导致无限运行 → P0 只允许单层，child request 不暴露 delegation capability。
- [Risk] usage evidence 不完整 → parent 明确标记 incomplete/needs_review，不伪造成本或预算结论。
- [Risk] 不同 idempotency key 并发读取同一剩余预算会共同放行 → parent-level lock/CAS 原子预留最坏情况预算；未知结果保持占用，可信 usage 到达后再结算。
- [Risk] 预算已预留但 event seq 容量不足，或反向只预留 event 容量 → claim、预算与 `0014` event capacity operation 在同一 application UoW 提交或回滚；local/service 复用同一接线，未知结果保持两类预约并阻止 terminal。
- [Risk] delegation final 被误标 terminal 或重试生成额外事件，会提前关闭 parent SSE、侵占 terminal reservation 或形成重复审计证据 → 在公共 CanonicalEvent/EventBus 边界双向校验 terminal type/flag/visibility，使用稳定三阶段 event id，并对 local/PostgreSQL/Redis reclaim 做顺序与零副作用反例。

## Migration Plan

在 trace migration `0013` 和 usage evidence outbox `0014` 之后以 `0015` 增加 `agent_delegations`、`delegation_budget_reservations` 与 `delegation_aggregates`（或经契约证明等价的受约束记录）及 repository，再接 application service，并让全新 claim、parent budget reservation 与 `0014` event capacity operation 在同一 UoW 内先于任何 child/queue/provider/业务 event 副作用提交；随后接 local runtime、service queue/worker，最后注册内置 `agent.delegate` 并原子切换 RUN-002。downgrade 同时要求 delegation/reservation/aggregation evidence 全空和 Alembic x 参数精确为 `allow_empty_evidence_downgrade=true`；缺失、重复、非法 opt-in 或任一 evidence 都在 DDL 前拒绝，保留读取兼容层且不删除 evidence。

## Open Questions

无阻塞问题；多层 graph 与跨租户策略留到 P1。
