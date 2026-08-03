## ADDED Requirements

### Requirement: 所有模型驱动工具调用使用tool-call唯一执行claim
普通和approved模型工具调用 SHALL在handler前以受信派生的`tool_call_id`建立唯一durable invocation claim，并绑定tenant/run/agent/trace、loop/turn、tool name、arguments/schema/catalog/action/resource digests和nullable approval identity。新模型驱动claim还 SHALL保存 `execution_lease_digest`、正整数 `execution_fence`、`execution_lease_expires_at`、nullable `handler_started_at` 与 `not_started_proof_json`。Approved调用 MUST同时满足既有`approval_id`唯一约束，且两个identity只能指向同一row。首次创建只能进入`claimed`；只有持有matching active lease/fence且已确认`claimed→executing`提交的owner MAY取得进程内`ToolExecutionPermit`并调用handler。

#### Scenario: 两worker竞争普通工具
- **WHEN** 两个worker并发执行同一tool_call_id
- **THEN** 只有一个claim winner调用handler，另一方读取exact state且调用计数不增加

#### Scenario: Approved identity不能创建第二row
- **WHEN** 相同approved intent分别按approval_id和tool_call_id竞争
- **THEN** 两个unique identity解析到同一invocation row和一次handler执行

#### Scenario: Binding冲突零handler调用
- **WHEN** 相同tool_call_id携带不同arguments/schema/catalog/action/resource或run identity
- **THEN** Registry返回replay conflict且不覆盖persisted claim

#### Scenario: Claimed lease换租后旧owner失效
- **WHEN** `claimed` row的原lease过期且新worker以CAS轮换lease digest、递增fence并保存可信未开始proof
- **THEN** 只有新lease/fence可以提交`claimed→executing`
- **AND** 旧owner在handler边界前被拒绝且调用计数为零

### Requirement: 工具claim状态决定唯一重放语义
新模型驱动tool invocation的execution state SHALL为`claimed|executing|completed|failed|needs_review`封闭联合。`claimed`表示尚未铸造执行许可；existing claimed只能在原lease过期后，以owner UoW/CAS原子保存`tool-handler-not-started-v1`、轮换lease digest并递增fence后继续。该proof exact fields为`schema_version/tool_call_id/binding_digest/prior_fence/next_fence/previous_lease_expires_at/reason/proof_digest`，`reason`只允许`claim_lease_expired`，digest必须从canonical preimage重算。只有matching active lease/fence可提交`claimed→executing`；提交确认后才铸造一次性`ToolExecutionPermit`，并在调用handler前再次核对permit与row identity。Completed exact replay SHALL返回既有result ref；确定failed exact replay SHALL返回既有稳定错误；executing、`claimed→executing`提交确认未知、handler可能已开始、result persistence/commit acknowledgement未知或字段矛盾 MUST单调提升needs-review。系统 MUST NOT把executing降回claimed、自动重调handler、把unknown改failed或actual-zero。

#### Scenario: Completed result只返回既有ref
- **WHEN**相同tool_call_id的completed claim再次进入call/call_approved
- **THEN** Registry返回persisted ToolCallResult/result ref且handler计数不增加

#### Scenario: Handler后提交确认未知
- **WHEN**handler返回但result/claim commit acknowledgement未知
- **THEN**claim和loop进入needs-review并保留event/capacity围栏
- **AND**恢复不再次调用handler

#### Scenario: Handler前可信失败可确定收口
- **WHEN**claim已创建但durable owner证明handler尚未取得执行权且preflight确定失败
- **THEN**同一claim可收口为failed并保存稳定错误
- **AND**不得创建新的tool_call_id重试

#### Scenario: Executing没有可信未开始旁路
- **WHEN** durable state为`executing`且没有completed/failed exact result
- **THEN** recovery进入`needs_review`并保留claim、lease、预算与event capacity
- **AND** 不接受超时lease、空`handler_started_at`或caller flag把它降回`claimed`

#### Scenario: Claimed到executing提交确认未知
- **WHEN** owner无法确认`claimed→executing`事务是否提交
- **THEN** 当前进程不得铸造`ToolExecutionPermit`或调用handler
- **AND** recovery读取实际durable state；`claimed`只能在lease过期后按proof换租，`executing`进入needs-review
