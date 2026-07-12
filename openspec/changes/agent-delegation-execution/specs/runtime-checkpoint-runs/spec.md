## ADDED Requirements

### Requirement: Runtime 执行单层 child run 并保持 parent 归属
runtime SHALL 通过受控 delegation application service 创建单层 child run。child MUST 继承 `tenant_id`、授权 identity 与 correlation refs，记录 `parent_run_id`、source/target agent 和 delegation id；local inline 与 service queue 路径 MUST 使用同一状态机和 repository contract。

#### Scenario: Local profile 执行 child
- **WHEN** 已授权 delegation 在 local profile 提交
- **THEN** runtime 创建并执行一个 child run，parent 可读取持久化关系与 terminal aggregation

#### Scenario: Service profile 投递 child
- **WHEN** 已授权 delegation 在 service profile 提交
- **THEN** 系统以稳定 operation/idempotency refs 投递一个 child run，worker 重投或 reclaim 不产生第二个逻辑 child

#### Scenario: Child failure 保留 parent 可审计结果
- **WHEN** target executor 失败或 child 被取消
- **THEN** child 进入对应 terminal，parent aggregation 记录失败状态与脱敏 error/trace refs，不把 parent 伪装成 delegation 成功

### Requirement: Delegation 幂等键绑定规范化请求
每个 delegation request SHALL 要求显式 idempotency key，并计算覆盖 tenant、有效 identity、parent run、source agent、target agent、child input 与有效预算的规范化 hash，随后持久绑定。同 key 同 hash MUST 重放既有 delegation/child 结果；同 key 异 hash MUST 返回 `delegation.idempotency_conflict`，且不得产生新 child、queue、provider 或业务 event 副作用。

#### Scenario: 同 key 同请求重放
- **WHEN** 调用方用相同 key 重试语义相同的规范化 delegation request
- **THEN** 系统返回既有 delegation 和 child refs，不再次执行 target executor

#### Scenario: 同 key 异请求冲突
- **WHEN** 相同 key 对应不同 target 或 input hash
- **THEN** 系统返回 409 `delegation.idempotency_conflict`，业务副作用计数为零

### Requirement: Delegation 失败使用封闭错误集合
delegation seam SHALL 使用 `delegation.edge_denied`、`delegation.policy_denied`、`delegation.idempotency_conflict`、`delegation.cycle_detected`、`delegation.depth_exceeded`、`delegation.budget_exceeded`、`delegation.target_not_found` 和 `delegation.execution_failed`。错误、event、audit 与 tool result MUST 脱敏；跨租户 target、provider raw usage、resume token 和本地路径不得进入结果。

#### Scenario: Target 不存在
- **WHEN** target agent 不存在或对当前 tenant/identity 不可见
- **THEN** seam 返回 `delegation.target_not_found`，不泄漏其他租户 agent 且不创建 child

#### Scenario: Child 执行失败
- **WHEN** child executor 达到确定性 failed terminal
- **THEN** seam 返回或记录 `delegation.execution_failed` 与脱敏 child/trace refs，parent aggregation 保留失败证据且不自动重复执行 child
