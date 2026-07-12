## ADDED Requirements

### Requirement: Approval 与 audit 继承 run canonical trace
Approval service SHALL 从已持久化 run execution context 取得非空 `trace_id`，并写入 ApprovalRecord、approval required/resolved event 和 audit evidence。HTTP/CLI body、policy result 或 tool metadata MUST NOT 覆盖该值；缺失 canonical trace MUST 在创建 approval 前 fail closed。

#### Scenario: 创建 approval 继承 run trace
- **WHEN** policy 为已绑定 canonical trace 的 run 返回 `require_approval`
- **THEN** waiting ApprovalRecord、checkpoint、required event 与 audit evidence 的 trace 逐值一致且不可为空

#### Scenario: Caller 不能覆盖 approval trace
- **WHEN** approve/deny 请求携带不同 trace 或相关 metadata 试图覆盖关联
- **THEN** service 忽略不受信覆盖并使用 persisted run trace，或在协议不允许该字段时返回 validation error，且不改变 approval 仲裁语义

#### Scenario: 缺失 run trace 时 fail closed
- **WHEN** legacy/损坏 run 在 backfill 完成后仍缺少 canonical trace并尝试创建 approval
- **THEN** service 在 checkpoint、approval、audit 和危险动作副作用前返回稳定错误
