## 1. 红灯合同与数据边界

- [ ] 1.1 为 edge/policy/tenant/cycle/depth/budget 拒绝、显式 key、同 key 异 hash、同 key 并发唯一 claim/reservation、claim 提交后崩溃重试、其他 key 改变 parent 余额后原 key 仍复用首次 reservation、同一 parent 不同 key 并发预算竞争、local/service 重放和 child failure 增加 red contract/integration tests，并证明拒绝路径零业务副作用
- [ ] 1.2 在 trace revision `0013` 与 usage outbox `0014` 后增加 `0015` delegation/reservation/aggregation migration、`agent_delegations`/`delegation_budget_reservations`/`delegation_aggregates` typed model、repository 与 UoW contract，覆盖 SQLite 与真实 PostgreSQL 的 parent-level row lock/CAS、并发幂等和预算预留；downgrade 覆盖空库无/非法/重复 opt-in 拒绝、精确 `-x allow_empty_evidence_downgrade=true` 允许，以及活跃/历史 evidence 即使 opt-in 也阻断，保留兼容读取且不删除 evidence

## 2. Application Service 与 Runtime

- [ ] 2.1 实现覆盖 tenant/identity/parent/source/target/child input/稳定预算意图的规范化 request hash 和 `DelegationService` 授权顺序；P0 无显式预算参数时使用 `inherit_parent`，动态 parent 余额和有效预留额不得进入 hash。无状态授权通过后，在同一事务中先按 `(tenant,parent,key)` 读取或创建 claim 并核对 hash，再只为全新 claim 在 parent lock/CAS 内计算和持久化最坏情况有效预算。既有同 hash 即使余额已变化也复用首次 reservation/operation，异 hash 在预算写入前冲突；pre-child 确定性失败才释放，child 已创建后按可信 usage 结算，未知结果保持 reserved/needs_review
- [ ] 2.2 实现 local 单层 child run、parent-child correlation 和 child terminal aggregation；token 在混合已知/未知时只累计已知值、全部未知时为 null，任一未知值都强制 `budget_status=incomplete` 且不得当 0；`cost_usd` 仅在全部 child cost 可用时求和，任一 unavailable 时为 null；`latency_ms` 仅在全部 child latency 已知时求和，任一未知时为 null；cost/latency 缺失都强制 `budget_status=incomplete`。增加 mixed known/unknown、all-null 以及 bool/负数/NaN/Infinity/cost-status mismatch tests；非法 evidence 在求和/结算前进入 needs_review，parent 已用预算不减少且可用余额不增加
- [ ] 2.3 接入 service Redis Streams/worker operation，验证 delivery/reclaim 不产生第二个逻辑 child 或重复 target executor

## 3. Tool/Module 与 RUN-002 契约

- [ ] 3.1 实现内置 `agent.delegate` tool/module request/result/error seam，从受信边界解析 source/tenant/identity/parent ownership，且不新增公开 delegation HTTP route
- [ ] 3.2 实现 API Contract 5.31 `RunDetailResponse`，把 RUN-002 route/schema/drift test 原子切换到持久化 parent/delegation aggregation
- [ ] 3.3 更新 app factory/runtime/worker 装配和 OpenAPI 无 delegation route 断言，不把 registry summary 当作执行证据

## 4. 验收证据

- [ ] 4.1 运行定向 unit/contract/integration/eval/local smoke，记录 AC-015/016 与 DLG-001 覆盖
- [ ] 4.2 运行真实 PostgreSQL/Redis service smoke，证明同一 parent 不同 key 预算竞争、其他 key 改变余额后原 key 稳定重放、并发幂等、queue reclaim、parent aggregation 与拒绝零副作用
- [ ] 4.3 完成该 capability 的 3 个 fresh code-reviewer Stage 1/2 PASS，保持 change 为 ready-to-archive 且不归档
