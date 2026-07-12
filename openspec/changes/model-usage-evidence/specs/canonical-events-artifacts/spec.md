## ADDED Requirements

### Requirement: Model usage CanonicalEvent 使用有界稳定 payload
`model.request.started` 与 `model.usage.updated` 或等价 embedding event SHALL 复用 CanonicalEvent envelope 和同一稳定 `usage_call_id`。`usage_call_id` MUST 在 provider 副作用前生成，并以非空 string 固定写入 `CanonicalEvent.payload.correlation.usage_call_id`；TelemetryFacade MUST 把同一值保留在 `TelemetryRecord.payload.correlation.usage_call_id`。系统 MUST NOT 新增 CanonicalEvent envelope 顶层字段或使用其他 payload 路径，也 MUST NOT 把该值放入 `ModelUsageEvidence` public DTO。Payload 其余内容 MUST 只包含 provider-neutral usage/decision/outcome 摘要；prompt、embedding 原文、vector 全文、headers、secret、provider raw response/client 和 raw exception MUST 被脱敏、替换为安全 ref 或阻止写入。

#### Scenario: Started 与 usage event 关联一致
- **WHEN** 同一次 model/embedding 调用发布 started 和 terminal usage event
- **THEN** 两个 event 的 tenant/run/request/agent/trace 与 `payload.correlation.usage_call_id` 逐值一致，TelemetryRecord 保留相同路径和值，各自 `event_id` 保持唯一，run 内 seq 单调递增且 terminal usage 恰好一条

#### Scenario: 大或敏感 provider payload 不内联
- **WHEN** adapter 输入包含 prompt、embedding、vector、secret 或 provider raw response
- **THEN** CanonicalEvent payload 不包含原值，只保留有界脱敏摘要或允许的 artifact/ref
