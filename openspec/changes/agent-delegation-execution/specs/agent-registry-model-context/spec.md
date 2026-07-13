## MODIFIED Requirements

### Requirement: Delegation edge 与摘要接缝默认受控
系统 SHALL 从 agent descriptor 读取 delegation edge，并提供显式校验 seam；未声明 edge 时默认拒绝 agent 互调。声明 edge 只授予进入 delegation application service 的资格；service MUST 在创建 child run 前继续校验 tenant、identity、policy、cycle、depth、budget 与幂等请求，并按 parent 原子预留预算。成功执行后，系统 MUST 从已经通过非 bool、非负、有限数值与 `cost_status` 组合校验的持久化 child run/model/trace evidence 生成 parent/child 归属与聚合摘要；非法 evidence 必须进入 fail-closed/needs_review，不得信任调用方自报 usage、budget 或 trace refs，也不得让负值反向冲减 parent 预算。

#### Scenario: 未声明 delegation edge 被拒绝
- **WHEN** agent A 请求委派给 agent B 且 A 的 descriptor 未声明 B
- **THEN** 系统返回 `delegation.edge_denied`，只保留脱敏 policy/audit evidence，不创建 child run、queue message、provider call 或业务事件

#### Scenario: 已声明 edge 仍需完整授权
- **WHEN** agent A 请求委派给已声明的 agent B
- **THEN** 系统在创建 child run 前校验同租户 identity、policy、cycle/depth、幂等绑定，并让所有 key 按 parent 原子竞争可用预算，任一失败均 fail closed

#### Scenario: Cycle、depth 或 budget 超限被拒绝
- **WHEN** delegation 会形成 cycle、超过 P0 单层深度或有效预算不足
- **THEN** 系统分别返回 `delegation.cycle_detected`、`delegation.depth_exceeded` 或 `delegation.budget_exceeded`，且零 child/queue/provider/业务事件副作用

#### Scenario: Delegated usage 从可信 evidence 归并
- **WHEN** child run 达到 terminal 且 token、cost、latency 与 trace evidence 已持久化
- **THEN** parent run 可读取包含 child run、usage、budget impact 和 trace refs 的聚合摘要；input/output token 在混合已知/未知时为所有已知 child 值之和，全部未知时为 null，任一 child token 为 null 都令 `budget_status=incomplete`；`cost_usd` 仅在所有 child cost 可用时求和，任一 unavailable 时为 null 并令 `budget_status=incomplete`；`latency_ms` 仅在所有 child latency 可用时求和，任一未知时为 null 并令 `budget_status=incomplete`；三者都不得把未知值当 0，也不需要业务 agent 拼接 provider 原始事件

#### Scenario: 混合已知与未知 child evidence 不伪造零值
- **WHEN** 同一 parent 的 child evidence 同时包含已知 token/cost/latency 与 null/unavailable 值
- **THEN** token 只累计已知值，cost/latency 按缺失规则为 null，`budget_status=incomplete`，聚合保留逐 child evidence refs 供复核

#### Scenario: 全部 child token 未知时保持 null
- **WHEN** 同一 parent 的全部 child input token 或 output token evidence 都为 null
- **THEN** 对应聚合 token 字段为 null 且 `budget_status=incomplete`，不得用 0 伪造已知总量

#### Scenario: 不完整 evidence 不伪造聚合
- **WHEN** child terminal 存在但必需 usage 或 trace evidence 缺失
- **THEN** delegation 标记为 `needs_review` 或 incomplete，并保留已有 refs，不伪造 cost、budget 或成功聚合

#### Scenario: 非法 child usage 不能反向冲减预算
- **WHEN** child durable evidence 含 bool、负 token/cost/latency、NaN/Infinity 或不一致的 `cost_usd/cost_status`
- **THEN** aggregation 在求和与 reservation 结算前 fail closed并把 delegation 标记为 `needs_review`，parent 已用预算不减少、可用余额不增加，错误不回显 raw provider value
