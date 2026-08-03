## ADDED Requirements

### Requirement: 模型工具审批绑定完整循环身份且不能扩权
模型工具 loop 的 approval SHALL 使用版本化 exact arguments/continuation，绑定 loop id、turn ordinal、tool call id、tool name、arguments/schema/catalog digests、action/resource、tenant/user/session/agent/run/request/trace、冻结 hard bounds 和原 policy decision。Approval record、checkpoint、grant hash、active resolution lease 与当前 bound context MUST 逐值及逐 digest 一致；grant SHALL 只能批准原 intent，不能增加工具、arguments、schema、预算或 deadline。

#### Scenario: Matching approval 恢复原 loop
- **WHEN** reviewer批准waiting tool intent且grant/lease/checkpoint/current bound context完全匹配
- **THEN** runtime恢复同一loop/turn/tool call并重新执行current hard bounds
- **AND** grant只跳过一次原soft policy gate

#### Scenario: 过期或扩权grant零工具副作用
- **WHEN** grant过期、已消费、跨tenant/run、替换arguments/schema/action/resource或提高hard bounds
- **THEN** resume在claim/handler/next model turn前关闭失败
- **AND** approval public状态和既有evidence不被伪造改写

### Requirement: Approval waiting 阻止模型循环和 run terminal
当模型工具 policy 返回 `require_approval` 时，系统 SHALL 持久化 approval、checkpoint 和 stable `approval.required` evidence，并使 loop/run 保持 waiting。Waiting 期间 MUST NOT 建立 tool execution claim、调用 handler、组装工具结果、进入下一 model turn 或发布 run terminal。Deny/resolution evidence SHALL 继续遵守既有 ordered outbox 与唯一 public status 规则。

#### Scenario: Waiting 期间零后续副作用
- **WHEN** approval 尚未被唯一仲裁并完成必要 evidence
- **THEN** model/tool/context调用计数保持在waiting前值
- **AND** run只暴露既有waiting approval摘要

#### Scenario: Deny 后不恢复循环
- **WHEN** deny赢得原子仲裁并完成ordered evidence
- **THEN** loop以确定失败收口且不调用handler或下一model turn
