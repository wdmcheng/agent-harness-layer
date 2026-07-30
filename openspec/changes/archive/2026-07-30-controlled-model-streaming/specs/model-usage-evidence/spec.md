## ADDED Requirements

### Requirement: 流式调用复用单一用量证据生命周期
每次流式调用 SHALL 在调用前生成一个稳定 `usage_call_id`，并复用既有 `model_usage` 的 2 槽位 started/final 生命周期。新 `text_stream` 调用的 durable started `ModelUsageEvidence.decision.usage_event_identity` MUST 精确为 `{"ref":"stream-usage","version":"v1"}`，并使用 `usage-stream:{usage_call_id}:s` 与 `usage-stream:{usage_call_id}:f` 作为 started/final event id；usage outbox 绑定 final id。`model.request.started` MUST 在 provider 副作用前发布；成功时 `model.usage.updated` MUST 在 `model.output.completed` 之后且运行终态之前发布。系统 MUST 使用 provider 最终结果携带的可信 usage，不得从 delta 数量、文本长度或零值推断用量。

#### Scenario: 成功流式调用的用量顺序
- **WHEN** provider 完成流式调用并返回可信 usage
- **THEN** started、所有 delta、completed、usage 按此顺序发布
- **AND** usage 与所有流事件具有同一 `usage_call_id`

#### Scenario: 未知结果不伪造用量
- **WHEN** provider 流被中断且无法证明远端停止或取得最终 usage
- **THEN** 系统不发布 `model.usage.updated`
- **AND** 不以 0、估算值、delta 计数或文本长度替代真实 usage

#### Scenario: 历史 usage identity 保持可恢复
- **WHEN** recovery 读取缺少 `stream-usage-v1` 的历史或非流式 durable usage row
- **THEN** 系统继续使用该 row 已绑定的 `usage:{tenant_id}:{usage_call_id}:started|final` identity 补投或校验
- **AND** 不把历史 event 重命名、重键或迁移为 stream identity

### Requirement: 流式结算保持预算与 lease 围栏
流式调用 SHALL 沿用既有预算预留、provider lease 与 attempt evidence。正常完成时，系统 MUST 使用最终 usage 结算预算并释放 lease。已证明停止时，只有 provider-neutral `ModelStreamCloseResult.usage.finality=complete` 且 token 与当前启用 cost 维度全部可信，才可生成中断 `ModelUsageEvidence` 并结算。stopped 或 unknown 的 null/partial usage MUST 在同一 UoW 把 usage outbox、对应 shared-budget claim/allocation 与 owner ledger 提升为 `needs_review`；usage row MUST 保留原 durable started evidence 和一个封闭 `attempt_review`，预算 result MUST 保存同一 review，且两者不得伪造 final evidence。review MUST 精确包含 close state、usage finality、原始受控 outcome/error、安全调用/时延摘要、单个 attempt 和 unknown budget charge；该 charge 的 token/cost 为 null、status 为 unknown、未决 ordinal 为 `[1]`。系统 MUST 保留 stream/usage 容量、reservation 与 lease，拒绝 exact replay 再次调用 provider，不发布 final usage 或 run terminal。不得因已发布部分 delta 就提前结算或释放。

#### Scenario: 正常完成后结算
- **WHEN** completed 与最终 usage 已可靠持久化并发布
- **THEN** 预算按可信 usage 结算且 provider lease 被释放
- **AND** 结算发生在运行终态之前

#### Scenario: 部分 delta 后状态未知
- **WHEN** 已发布部分 delta 但 provider 结果未知
- **THEN** 预算 reservation 与 provider lease 保持未决
- **AND** attempt evidence 记录 `side_effect_state=unknown`，运行终态被阻止

#### Scenario: stopped usage 维度不完整
- **WHEN** close result 为 stopped，但 token 任一项未知或 cost-enabled route 缺少可信 cost
- **THEN** observed 值只进入 attempt evidence，调用级 budget charge 保持 unknown
- **AND** usage outbox、预算 operation 与 owner ledger 在同一事务进入 needs_review
- **AND** 系统不发布最终 usage、不释放 stream/usage 容量或 lease、不允许运行终态

#### Scenario: needs-review exact replay 不重启供应商
- **WHEN** 同一稳定流式调用再次命中已持久化的 attempt review
- **THEN** 系统逐值校验 usage 与预算保存的是同一 review
- **AND** 返回 needs-review 重放错误，不再次 prepare、迭代或重启 provider stream
