## MODIFIED Requirements

### Requirement: Delegation edge 与摘要接缝默认受控
系统 SHALL 从 agent descriptor 读取 delegation edge，并提供显式校验 seam；未声明 edge 时默认拒绝 agent 互调。声明 edge 只授予进入 delegation application service 的资格；service MUST 在创建 child run 前继续校验 tenant、identity、policy、cycle、depth、budget 与幂等请求。成功执行后，系统 MUST 从持久化 child run/model/trace evidence 生成 parent/child 归属与聚合摘要，不得信任调用方自报 usage、budget 或 trace refs。

#### Scenario: 未声明 delegation edge 被拒绝
- **WHEN** agent A 请求委派给 agent B 且 A 的 descriptor 未声明 B
- **THEN** 系统返回 `delegation.edge_denied`，只保留脱敏 policy/audit evidence，不创建 child run、queue message、provider call 或业务事件

#### Scenario: 已声明 edge 仍需完整授权
- **WHEN** agent A 请求委派给已声明的 agent B
- **THEN** 系统在创建 child run 前校验同租户 identity、policy、cycle/depth、预算和幂等绑定，任一失败均 fail closed

#### Scenario: Cycle、depth 或 budget 超限被拒绝
- **WHEN** delegation 会形成 cycle、超过 P0 单层深度或有效预算不足
- **THEN** 系统分别返回 `delegation.cycle_detected`、`delegation.depth_exceeded` 或 `delegation.budget_exceeded`，且零 child/queue/provider/业务事件副作用

#### Scenario: Delegated usage 从可信 evidence 归并
- **WHEN** child run 达到 terminal 且 token、cost、latency 与 trace evidence 已持久化
- **THEN** parent run 可读取包含 child run、usage、budget impact 和 trace refs 的聚合摘要；input/output token 为所有已知 child 值之和，任一 child token 为 null 时 `budget_status=incomplete` 且不得把未知值当 0，不需要业务 agent 拼接 provider 原始事件

#### Scenario: 不完整 evidence 不伪造聚合
- **WHEN** child terminal 存在但必需 usage 或 trace evidence 缺失
- **THEN** delegation 标记为 `needs_review` 或 incomplete，并保留已有 refs，不伪造 cost、budget 或成功聚合
