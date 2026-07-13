## MODIFIED Requirements

### Requirement: TelemetryFacade 统一观测输出边界
系统 SHALL 暴露 `TelemetryFacade`，用于接收 CanonicalEvent 或 provider-neutral telemetry record，并统一完成 trace context 合并、payload redaction 和外部 provider fan-out。Facade MUST NOT 向 API、CLI、runtime、template app 或业务 agent 暴露 provider SDK client、transport object、raw span、raw response 或未脱敏异常。对 ordinary provider-neutral record，Facade 继续负责 local/jsonl evidence 写入；对 `model.request.started`、`model.usage.updated` 这类 canonical usage event，EventBus SHALL 是唯一 local durable 写入者，并在持久化成功后把同一个 event 交给 Facade。Facade 接收已持久化 usage event 时 MUST 只做 provider fan-out，MUST NOT 再写 local sink、创建第二条 CanonicalEvent 或修改既有 usage。provider degraded status 通过有界的 facade result 和独立、非 usage、幂等的 provider-status evidence 表达，不得追加第二条调用级最终 usage；`model.usage.updated` 的 `CanonicalEvent.terminal` MUST 为 false。

#### Scenario: Ordinary telemetry 未配置 provider 仍写入 local evidence
- **WHEN** 调用方通过 TelemetryFacade 发布非 canonical usage 的 runtime、tool、retrieval、eval、approval 或 audit provider-neutral record，且未配置外部 provider
- **THEN** Facade 把脱敏 record 写入 local/jsonl sink，并可按 `run_id` / `trace_id` 读取

#### Scenario: 未配置 provider 仍可读取 usage
- **WHEN** model/embedding 调用完成且未配置任何 SaaS observability provider
- **THEN** local sink 可按 tenant/run/trace/`usage_call_id` 读取完整 provider-neutral usage 摘要，且 public `ModelUsageEvidence` 不新增同义 correlation 字段

#### Scenario: Provider fan-out 失败不丢 local evidence
- **WHEN** local usage 已持久化后 OTel、Logfire、Phoenix 或 Langfuse adapter 失败
- **THEN** local usage 保持可读，Facade 返回不含 secret/raw exception 的 degraded status；需要持久化时只写独立、非 usage、幂等的 provider-status evidence，不修改 local usage、不产生第二条调用级最终 usage

#### Scenario: Local 持久化失败不伪装已结算
- **WHEN** local usage sink 在 provider 已产生或可能产生调用副作用后失败
- **THEN** durable settlement 保留已持久化的脱敏 provider 结果与稳定 usage event id，系统返回封闭的 pending/needs-review 状态，不报告 usage 已 durable；recovery 只补投同一 usage event，不自动重试不确定 provider 副作用

#### Scenario: Local 写后确认丢失不重复结算
- **WHEN** local usage sink 已接受稳定 event id但调用方在确认前退出
- **THEN** recovery 以相同 event id幂等重放 dispatch并收口 settlement，最终 usage 恰好一条，provider 调用次数保持一次

#### Scenario: 直接发布 usage 不能绕开唯一写入者
- **WHEN** 调用方尝试让 TelemetryFacade 直接创建或 local-write `model.request.started` / `model.usage.updated`
- **THEN** Facade 拒绝该调用或要求传入 EventBus 已持久化的 CanonicalEvent，不写 local sink、不创建第二条 event

#### Scenario: Provider fan-out 不泄漏 SDK object
- **WHEN** facade 将 telemetry record 发送给外部 provider adapter
- **THEN** adapter 只收到 provider-neutral DTO 和脱敏 payload，不收到 provider SDK object、ORM model、raw exception 或业务对象
