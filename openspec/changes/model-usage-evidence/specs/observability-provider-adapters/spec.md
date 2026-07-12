## ADDED Requirements

### Requirement: Usage evidence 先持久化 local 再 fan-out
TelemetryFacade SHALL 在任何外部 provider fan-out 前把脱敏后的 `ModelUsageEvidence` 写入 local durable evidence。外部 provider 成功或失败 MUST NOT 改写、删除、隐藏或重复结算 local evidence；provider failure 只能追加脱敏 degraded status。

#### Scenario: 未配置 provider 仍可读取 usage
- **WHEN** model/embedding 调用完成且未配置任何 SaaS observability provider
- **THEN** local sink 可按 tenant/run/trace/`usage_call_id` 读取完整 provider-neutral usage 摘要，且 public `ModelUsageEvidence` 不新增同义 correlation 字段

#### Scenario: Provider fan-out 失败不丢 local evidence
- **WHEN** local usage 已持久化后 OTel、Logfire、Phoenix 或 Langfuse adapter 失败
- **THEN** local usage 保持可读且仅追加不含 secret/raw exception 的 degraded summary，不产生第二条 terminal usage

#### Scenario: Local 持久化失败不伪装已结算
- **WHEN** local usage sink 在 provider 已产生或可能产生调用副作用后失败
- **THEN** 系统返回封闭的未完整结算状态或错误并保留安全关联证据，不报告 usage 已 durable，也不自动重试不确定 provider 副作用
