## ADDED Requirements

### Requirement: 模型驱动工具调用复用统一执行与输出边界
模型工具循环 SHALL 只通过 `ToolRegistry` 的 resolve、`call` 与 `call_approved` seam 执行工具。Registry SHALL 在 execution claim/handler 前重验 intent/canonical arguments/schema/catalog/Agent allowlist/action/resource，复用既有 PolicyEngine、audit、workspace、MCP、artifact、secret redaction 和 output guard。Loop 或 adapter MUST NOT 直接持有 handler、BuiltinTool、MCP client 或 workspace implementation。

#### Scenario: Allow 路径仍经过 Registry 全部门禁
- **WHEN** 模型 intent 合法且 policy allow
- **THEN** handler 只由 Registry 在 schema/allowlist/policy/capacity/claim 成功后调用
- **AND** 结果使用既有 `ToolCallResult` 和 output guard

#### Scenario: Loop 不能调用 Registry 私有 handler
- **WHEN** loop 或 adapter 尝试从 resolve DTO、descriptor 或 service mapping取得 callable
- **THEN** 公共 DTO/类型边界拒绝该对象且工具执行为零

### Requirement: Approved 工具恢复绑定原模型意图
`call_approved` SHALL 要求 `ApprovalGrant`、checkpoint、ToolIntent、ResolvedToolIntent 与当前 bound context 对 loop id、turn ordinal、tool call id、tool name、arguments digest、schema/catalog、action/resource、tenant/identity/run/agent/request/trace逐值一致，并保留既有 approval-id唯一 claim。任一字段缺失、额外、漂移、跨 run 或 lease 失效 MUST 在 handler 前拒绝。

#### Scenario: Matching grant执行一次
- **WHEN** durable approval/checkpoint与当前intent、active grant/lease全部匹配且没有completed claim
- **THEN** Registry先建立或取得唯一claim再执行handler一次并保存result ref

#### Scenario: 同步篡改record与checkpoint仍失败
- **WHEN** caller同步改写approval record和checkpoint中的tool/arguments/catalog identity但与原canonical intent不一致
- **THEN** Registry从受信loop state重算后拒绝且handler计数为零
