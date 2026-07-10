## ADDED Requirements

### Requirement: Approved tool continuation 使用绑定 grant 和持久化单次执行 claim
`ToolRegistry` SHALL 提供只供 approval resume 使用的受控调用 seam，接收已由 `ApprovalService` 验证的 `ApprovalGrant`。grant MUST 绑定 approval、tenant、identity、agent、run、tool action/resource 和 arguments hash；registry MUST 在调用 handler 前创建以 `approval_id` 唯一的持久化 execution claim，完成后保存 result ref。

#### Scenario: 匹配 grant 执行一次并保存结果
- **WHEN** approved continuation 的 grant 与 checkpoint/tool request 全部字段匹配且不存在 execution claim
- **THEN** registry 先持久化 claim，再调用 handler 一次，保存 completed result ref 和 audit/trace evidence

#### Scenario: 不匹配 grant 被拒绝
- **WHEN** approval、tenant、identity、agent、run、action、resource 或 arguments hash 任一不匹配
- **THEN** registry 返回稳定 approval/policy denial，handler 执行计数为零，不创建 completed invocation

#### Scenario: 已完成 claim 返回既有结果
- **WHEN** 相同 `approval_id` 的 completed execution claim 再次进入 resume seam
- **THEN** registry 返回已持久化 result ref，不再次调用 handler

#### Scenario: 未完成 claim 不自动重放副作用
- **WHEN** 相同 `approval_id` 存在 `executing` claim 但没有 completed result ref
- **THEN** registry 返回 `needs_review`/稳定中断状态并保留证据，不自动重试 handler

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
