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
每个 delegation request SHALL 要求显式 idempotency key，并计算覆盖 tenant、有效 identity、parent run、source agent、target agent、child input 与稳定预算意图的规范化 hash。P0 request 没有显式预算参数时，预算意图 MUST 固定为 `inherit_parent`；动态 parent 剩余额度、锁内计算的有效预留额与其他可变余额投影 MUST NOT 进入 hash。ownership/edge/policy/tenant/cycle/depth 校验不得创建 delegation/预算/child 业务状态；通过后，系统 MUST 在同一事务中先按唯一 `(tenant_id,parent_run_id,idempotency_key)` 读取或创建 claim 并核对 hash：既有同 hash MUST 重放或恢复首次持久化的 delegation/child/reservation，不得按当前余额重算 hash 或再次预留；既有异 hash MUST 在任何 reservation 写入前返回 `delegation.idempotency_conflict`；全新 claim 才在 parent lock/CAS 内计算最坏情况有效预留额，并与 parent budget reservation 同事务提交或回滚。任何冲突或失败不得产生新 child、queue、provider 或业务 event 副作用；允许的一次脱敏 policy/audit evidence 不属于 delegation 业务状态。

#### Scenario: 同 key 同请求重放
- **WHEN** 调用方用相同 key 重试语义相同的规范化 delegation request
- **THEN** 系统返回既有 delegation 和 child refs，或从原 claim/reservation 的 durable state 恢复原 operation；不创建第二 reservation、不再次执行 target executor

#### Scenario: 同 key 异请求冲突
- **WHEN** 相同 key 对应不同 target 或 input hash
- **THEN** 系统在预算读取/预留前通过 tool/module error DTO 返回 `delegation.idempotency_conflict`，新 claim/reservation 与业务副作用计数均为零；P0 没有 delegation HTTP response，未来 HTTP adapter 如需映射 status 必须由独立公开契约定义

#### Scenario: 同 key 并发只提交一个 claim 与 reservation
- **WHEN** 两个并发请求使用相同 key 与相同规范化 hash，且该 key 尚未持久化
- **THEN** SQLite 与 PostgreSQL repository 都只提交一个 claim 和一个 parent reservation；另一请求重放或恢复该 durable state，不重复占用余额

#### Scenario: Claim 后崩溃重试复用原 reservation
- **WHEN** 新 claim 与 reservation 已提交，但进程在创建 child 前退出，随后相同 key/hash 重试
- **THEN** recovery 复用原 claim、reservation 与 operation继续执行或确定性补偿，不再次预留、不因余额变化错误返回 budget exceeded

#### Scenario: 其他 key 改变余额后原 key 仍稳定重放
- **WHEN** 首次 claim/reservation 已提交，另一 idempotency key 随后预留或结算同一 parent 预算，再以原 key 和相同稳定请求重试
- **THEN** 系统按稳定 request hash 命中原 claim 并复用首次持久化的有效 reservation/operation，不把当前 parent 剩余额度写入 hash，不返回 `delegation.idempotency_conflict`，也不创建第二 reservation

### Requirement: Delegation 预算按 parent 原子预留与结算
系统 SHALL 在任何 child run、queue、provider 或业务 event 副作用前，以 parent run 为竞争范围，通过 row lock 或等价 CAS 原子预留全新 claim 的最坏情况有效预算；新 claim 与 reservation MUST 在同一事务提交或回滚。不同 idempotency key MUST 竞争同一 parent 可用余额，不能各自读取旧余额后同时放行。reservation MUST 持久化 `reserved|settled|released|needs_review` 状态：child 创建前的确定性失败可原子释放；child 创建后只能用已经通过非 bool、非负、有限数值与 cost-status 组合校验的可信 usage evidence 结算；非法或结果未知时 MUST 保持占用并进入 `needs_review`，不得把未知值当 0 或用负值增加可用余额。

#### Scenario: 不同 key 并发不能共同超支
- **WHEN** 两个不同 idempotency key 并发请求同一 parent，单个请求都低于当前余额但二者最坏情况预算之和超过余额
- **THEN** SQLite 与 PostgreSQL repository 都只允许一个 reservation 成功；另一请求返回 `delegation.budget_exceeded`，不创建 child、queue、provider call 或业务 event

#### Scenario: Child 创建前失败释放预留
- **WHEN** reservation 成功后、child 创建前发生可证明的确定性失败
- **THEN** 同一事务或受 fencing 的补偿把 reservation 标记为 released并归还余额，重试不产生重复释放

#### Scenario: Child 结果未知时保留预留
- **WHEN** child 已创建但 execution/usage 结果不确定或必要 usage evidence 缺失
- **THEN** reservation 保持 reserved或转为 needs_review，parent 可用余额不增加；只有可信 usage evidence 可把它结算为 settled

### Requirement: Delegation 失败使用封闭错误集合
delegation seam SHALL 使用 `delegation.edge_denied`、`delegation.policy_denied`、`delegation.idempotency_conflict`、`delegation.cycle_detected`、`delegation.depth_exceeded`、`delegation.budget_exceeded`、`delegation.target_not_found` 和 `delegation.execution_failed`。错误、event、audit 与 tool result MUST 脱敏；跨租户 target、provider raw usage、resume token 和本地路径不得进入结果。

#### Scenario: Target 不存在
- **WHEN** target agent 不存在或对当前 tenant/identity 不可见
- **THEN** seam 返回 `delegation.target_not_found`，不泄漏其他租户 agent 且不创建 child

#### Scenario: Child 执行失败
- **WHEN** child executor 达到确定性 failed terminal
- **THEN** seam 返回或记录 `delegation.execution_failed` 与脱敏 child/trace refs，parent aggregation 保留失败证据且不自动重复执行 child
