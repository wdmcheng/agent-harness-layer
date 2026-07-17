## ADDED Requirements

### Requirement: Shared-budget claim 是 terminal 前置 evidence
Shared-budget reservation/settlement SHALL 与既有 usage/delegation evidence 关联，但 MUST NOT 复用或篡改 event capacity 数值。任一未结算 shared-budget claim MUST 阻止 run terminal。预算拒绝允许写入唯一、稳定、脱敏且封闭的内部 decision/audit/usage rejection evidence；provider、child、queue、业务执行与 delegation 生命周期 event 副作用 MUST 为零。

新 direct operation 的 shared claim、usage outbox 与 event-capacity reservation，以及新 delegation 的 shared claim、既有 delegation reservation、ordered evidence 与 event-capacity reservation，MUST 分别在同一 application UoW 全部提交或回滚。可信结果持久化、`side_effect_state=result_committed`与 shared settlement MUST 原子提交；cache hit的zero-impact claim/allocation、usage result/outbox与capacity结算也必须原子，不能产生单边记录。提交后的event publish失败只补投既有outbox，不得重放外部副作用。

#### Scenario: Budget 拒绝只留下允许的内部证据
- **WHEN** shared ledger 在外部副作用前拒绝 direct 或 delegation operation
- **THEN** 系统最多写合同允许的稳定内部 rejection evidence，不发布 delegation claimed/child/final，不调用 provider、不创建 child、不投递 queue，event capacity 与 token/cost ledger 分别保持各自不变量
