## 1. 红灯合同与数据边界

- [ ] 1.1 为 edge/policy/tenant/cycle/depth/budget 拒绝、显式 key、同 key 异 hash、local/service 重放和 child failure 增加 red contract/integration tests，并证明拒绝路径零业务副作用
- [ ] 1.2 增加 delegation/aggregation migration、typed model、repository 与 UoW contract，覆盖 SQLite 与真实 PostgreSQL 并发幂等

## 2. Application Service 与 Runtime

- [ ] 2.1 实现覆盖 tenant/identity/parent/source/target/child input/有效预算的规范化 request hash、持久化 idempotency claim 和 `DelegationService` 授权顺序
- [ ] 2.2 实现 local 单层 child run、parent-child correlation 和 child terminal aggregation；token 只累计已知值，任一未知值强制 `budget_status=incomplete` 且不得当 0，其他缺失 evidence 进入 incomplete/needs_review
- [ ] 2.3 接入 service Redis Streams/worker operation，验证 delivery/reclaim 不产生第二个逻辑 child 或重复 target executor

## 3. Tool/Module 与 RUN-002 契约

- [ ] 3.1 实现内置 `agent.delegate` tool/module request/result/error seam，从受信边界解析 source/tenant/identity/parent ownership，且不新增公开 delegation HTTP route
- [ ] 3.2 实现 API Contract 5.31 `RunDetailResponse`，把 RUN-002 route/schema/drift test 原子切换到持久化 parent/delegation aggregation
- [ ] 3.3 更新 app factory/runtime/worker 装配和 OpenAPI 无 delegation route 断言，不把 registry summary 当作执行证据

## 4. 验收证据

- [ ] 4.1 运行定向 unit/contract/integration/eval/local smoke，记录 AC-015/016 与 DLG-001 覆盖
- [ ] 4.2 运行真实 PostgreSQL/Redis service smoke，证明并发幂等、queue reclaim、parent aggregation 与拒绝零副作用
- [ ] 4.3 完成该 capability 的 3 个 fresh code-reviewer Stage 1/2 PASS，保持 change 为 ready-to-archive 且不归档
