## Source Links

- Product-Spec.md：只读引用 `REQ-007` 受控 delegation、`REQ-012` 模型与预算、`REQ-014` terminal 前置 evidence；共享 parent 预算的新行为与验收由本 change delta 定义，待后续显式 sync/archive 流程再合并主规格。
- DEV-PLAN.md：只读引用 Phase 13.7 usage evidence、Phase 13.8 delegation reservation 与 Phase 13.9 顺序；本 change 作为临时 Phase 13.8A 前滚，待后续显式主线同步再更新计划正文。
- API-Contract.md：5.10 `AgentDescriptor.budget`、5.29 `ModelUsageEvidence`、5.30 `DelegationSummary`、DLG-001 与 MOD-001。
- Design-Brief.md 或设计稿：不涉及 UI；本 change 不读取或修改架构图。
- CONTEXT.md / ADR：无新增领域术语或架构决策来源。

## Why

现有合同分别封闭了 direct model/embedding usage evidence 与 delegation budget reservation，却没有让两者竞争同一个 parent run token/cost 硬上限。若 delegation 已预留额度，后续 direct provider 调用仍可能按旧余额放行，导致 parent execution tree 合计超支。

## What Changes

- 将 `max_tokens_per_run` 与 `max_cost_usd_per_run` 收紧为 root/parent execution tree 的共享硬上限；token 固定按可计费 input + output 计算，embedding 只计 input。Token 维度始终启用，cost 维度仅在 `max_cost_usd_per_run` 非 null 时启用；cost 关闭时合法 `cost=null/cost_status=unavailable` 不产生 cost impact 或 needs_review，但所有非法数值/status 组合仍须拒绝。
- 把 `policy review threshold` 与共享硬上限分层：软阈值只触发 fallback/`require_approval`，审批不得提高、重置或覆盖 hard limit；只有 hard-eligible operation 才能进入审批，批准后仍须按当前余额原子 reservation。
- Root run 在创建且任何业务副作用前冻结 hard limit、适用 descriptor/budget/config version 与允许 route 的 price source/version；reload 只影响新 root run，child 继承 parent snapshot，fallback 只能在冻结配置内按实际 route 重算 reservation。`0016` 只为仍需继续执行且已有 durable immutable version 引用的 legacy tree 回填 snapshot；已完全 terminal、全部 evidence 已封闭的 legacy tree 保留原 `0014`/`0015` 历史且不伪造 ledger。缺少快照的在途或待恢复 tree 必须先由旧 writer drain/reconcile，否则在 DDL/UPDATE 前整批拒绝。
- 新增 parent shared budget ledger，并以内部非空 `budget_owner_run_id` 唯一标识 execution-tree root：root operation 取自身 `run_id`，delegated child 通过 tenant-fenced 唯一 delegation relation 解析到同一 root；不得复用 root 上为 null 的 `AgentRun.parent_run_id` 作为 ledger key。Direct model、direct embedding 与 delegation 顶层 claim 在同一 owner row lock/CAS 下预约、结算、释放或进入 `needs_review`。
- provider、child 或 queue 外部副作用前，系统从受信 router/adapter/descriptor/policy 边界计算有限最坏 token/cost 预约；业务输入不得自报或缩小预约。配置了硬上限却无法证明有限上界时，在外部副作用前 fail closed。
- direct claim 必须扣除 active delegation reservation；delegation claim 必须扣除 active direct reservation；两个 direct claim 与混合并发同样竞争统一余额。
- 可信实际 usage 在同一原子边界替换原 reservation；确定性零外部副作用失败才可释放，未知或非法结果保持保守占用并进入 `reserved|needs_review`。
- Tenant-fenced纯读embedding cache lookup可先于reservation；确定hit保留null/unavailable、`provider_called=false` usage evidence，该null表示provider usage不适用而非unknown。Root/direct hit以稳定`usage_call_id`建立或复用settled/zero-impact direct claim；delegated child hit只在既有delegation下建立或复用settled/zero-impact allocation，不建立parent direct claim。两者都不占余额或阻止terminal；miss仍必须在provider前预约。
- child model/embedding 调用只在既有 delegation reservation 内取得受约束 allocation，parent ledger 不把同一 child usage 作为 direct charge 再计一次。
- 使用稳定 `usage_call_id` / delegation claim 关联幂等恢复，禁止重复预约、provider call、child run 或 queue operation；任一未结算共享预算 claim 阻止 parent terminal。
- 新 direct operation 必须把 `0016` direct claim 与 `0014` usage settlement/event-capacity reservation 放入同一 application UoW；新 delegation 必须把 `0016` top-level claim、`0015` relation/reservation 与 `0014` ordered evidence/event-capacity reservation 放入同一 UoW。任一 ledger、capacity、唯一键或 relation 检查失败时整组回滚；可信 provider/child result 的持久化与 shared-ledger settlement 同样原子提交。
- Direct model/embedding 的无可信有限上界、静态硬不合格、当前余额不足、snapshot 无效或 owner 处于 needs-review 都统一返回封闭错误码 `budget.reservation_rejected`；细分原因只允许写入脱敏内部 evidence，不公开余额或限额。Delegation 预算不足继续沿用 `delegation.budget_exceeded`。
- 外部副作用前先把claim的durable phase推进为`side_effect_state=started`。恢复只允许三种新writer状态：effect未开始；effect可能开始但没有原子result+settlement；result+settlement已提交而event待补投。新writer不得产生result-only、ledger-only或cache claim/evidence单边状态。
- 组合检查顺序固定为exact replay/identity conflict、authorization/owner/relation/snapshot、`event.sequence_state_invalid`、hard budget、`event.sequence_exhausted`、unique-race重读。Budget与capacity同时失败时返回budget code；capacity-only始终保留前置`event.sequence_exhausted`。
- 在 `0014` 与 `0015` 后以前滚 revision `0016` 增加共享 ledger/claim/allocation；不改写既有 migration，SQLite 与 PostgreSQL 遵守相同状态机和结果。
- 拒绝路径允许封闭、脱敏的内部 decision/audit/usage rejection evidence，但 provider、child、queue 与业务执行副作用必须为零。

## Non-Goals

- 不改写 `0014` usage evidence/event capacity 或 `0015` delegation relation/reservation/aggregation 的历史职责。
- 本轮不直接修改 canonical `openspec/specs/**`、历史/前置 active change 或 Product-Spec/API-Contract/DEV-PLAN 主线正文；这些材料仅作为只读上游，主规格合并留给后续显式 sync/archive 流程。
- 不新增公开预算 HTTP endpoint 或公开 DTO 同义字段，不持久化 prompt、embedding 原文、provider raw payload、secret 或动态余额。
- 不实现 Phase 13.9 SSE transport、Phase 14/15、archive、push、发布或部署。
- 不把 event capacity reservation 当作 token/cost budget ledger，也不扩展为多层或跨租户 delegation。

## Capabilities

### New Capabilities

- `shared-parent-budget-ledger`：定义 parent execution tree 的共享 token/cost ledger、direct/delegation claim、child allocation、原子结算、幂等恢复和拒绝语义。

### Modified Capabilities

- `agent-registry-model-context`：把 agent budget 从单个调用方局部阈值收紧为 parent execution tree 共享硬上限，并要求受信最坏情况预算。
- `auth-policy-hitl-approvals`：把单次调用 review threshold 固定为不可绕过 shared hard limit 的软策略层，并定义 approval 前后预算重检顺序。
- `runtime-checkpoint-runs`：让 direct/delegation claim 的恢复、unknown 状态与 parent terminal 使用同一 fencing 语义。
- `canonical-events-artifacts`：明确共享预算拒绝允许的内部 evidence，以及未结算 claim 阻止 terminal；不改变 event capacity 账本。
- `storage-migration-uow`：增加 `0016` ledger/claim/allocation、backfill、SQLite/PostgreSQL 一致性与 evidence-aware downgrade。
- `service-app-shell`：runtime/worker composition 必须注入统一 shared-budget repository/UoW，不新增公开 route。
- `service-deployment-boundaries`：local 与真实 PostgreSQL/Redis 验证混合预算竞争、恢复和零外部副作用。

## Impact

- API/配置语义：`AgentDescriptor.budget` 由模糊“单 run”预算收紧为 parent execution tree 硬上限；公开字段形状不变。
- 数据：新增 `0016` shared parent ledger、operation claim 与 delegation child allocation，以非空 `budget_owner_run_id` 关联 execution-tree root，并关联既有 `usage_call_id` 和 delegation reservation。
- runtime：model/embedding invocation、delegation claim、worker recovery、terminal guard 与 UoW composition 共享预算所有权。
- 测试：覆盖 delegation→direct、direct→delegation、direct→direct、真并发、token/cost、cache hit、崩溃窗口、unknown/非法 usage、child 不双计、SQLite/PostgreSQL 一致与真实 Redis worker 恢复。
