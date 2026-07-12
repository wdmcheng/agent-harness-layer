## Context

当前 `ModelResponse` 只有自由形状 `token_usage` 与 `latency_ms`，embedding cache 只记录 provider latency；EventBus 已有 `model.request.started`/`model.usage.updated` 类型，TelemetryFacade 已提供 local-first、redaction 与 provider fan-out，但没有一份统一的 usage DTO 或强制关联路径。Phase 13.8 将依赖 durable child usage 做 parent aggregation，因此本 change 必须先让单次 model/embedding 调用产生不可由业务 agent 伪造的 provider-neutral evidence。

## Goals / Non-Goals

**Goals:**

- 统一 model 与 embedding 的 token、cost、latency、route/fallback/budget 字段和可用性语义，并逐字段遵守 API Contract 5.29。
- 在 adapter/router 组合边界注入 tenant/run/request/agent/trace 关联，并先写 local durable evidence。
- 让完成、受控拒绝和 provider 失败都留下脱敏、可结算证据；用 fake provider 提供稳定的 5 秒入口时延门禁。

**Non-Goals:**

- 不执行 delegation 或聚合 parent/child，不新增 SSE transport。
- 不持久化 prompt/embedding 原文、provider raw response/client，不依赖真实 provider 或 SaaS telemetry。

## Decisions

1. **实现现有 provider-neutral DTO，不扩张字段。** `ModelUsageEvidence` 由 adapter/router 边界构造，字段严格为 API Contract 5.29 的 `usage_kind`、tenant/provider/model、nullable input/output tokens、`cost_usd`、`cost_status`、`latency_ms`、decision、run/agent、可选 request 和必填 trace。生命周期 outcome/error/timestamp 使用稳定 event type 与有界 payload 表达；单次调用 `usage_call_id` 的唯一 CanonicalEvent 路径固定为 `payload.correlation.usage_call_id`，TelemetryFacade 映射后固定为 `TelemetryRecord.payload.correlation.usage_call_id`。两处都是非空 string，不新增 CanonicalEvent envelope 顶层字段，也不进入 `ModelUsageEvidence`。替代自由路径或新增第二套字段都会造成契约漂移，因此拒绝。
2. **缺失与零值分开表达。** token 不可用使用 null，不伪造 0；cost 只有 provider 报告或来自带版本/来源的可验证 price configuration 才能写入 `cost_usd`，分别标 `reported` 或 `estimated`，否则为 null + `unavailable`。estimated 的安全来源落在既有 `decision.price_source_ref` 与 `decision.price_source_version`，不新增顶层 DTO 字段，也不内联完整价目。币种由字段名固定为 USD。
3. **started 与 terminal usage 共用事件关联。** 每次调用在 adapter 副作用前生成稳定 `usage_call_id`，并与 `run-trace-correlation` 提供的 canonical trace 一起发布 `model.request.started`；完成、budget/policy 拒绝或 provider 失败后发布恰好一条 terminal `model.usage.updated` 或等价 embedding event。`usage_call_id` 只存在于 `CanonicalEvent.payload.correlation.usage_call_id`，并由 TelemetryFacade 原样映射到 `TelemetryRecord.payload.correlation.usage_call_id`；不得使用 envelope 顶层字段、`payload.usage_call_id` 或 `payload.metadata.usage_call_id` 等替代路径。Embedding 使用同一 DTO，以 `usage_kind=embedding` 区分。
4. **local-first 是提交顺序。** CanonicalEvent/EventBus 或等价 local sink 成功持久化脱敏 evidence 后，TelemetryFacade 才进行可选 provider fan-out。外部 provider failure 只追加 degraded summary，不回滚或覆盖 local evidence。若 local durable write 失败，调用结果不得被表述为已完整结算，返回封闭错误或 needs-review 等确定性状态。
5. **失败 evidence 有界且可结算。** provider 异常映射到稳定 error code/outcome；保留 latency、已知 usage、route/budget decision 和关联字段，移除 prompt、embedding input、headers、secret、raw exception/response。Redaction 在 CanonicalEvent 与 telemetry 两个出口都执行，防止未来 sink 绕过。
6. **性能门禁测入口，不测随机微步骤。** `scripts/smoke_local.py` 使用 fake provider 从公开入口到唯一 terminal 记录 monotonic 总时延并断言小于 5 秒；fixture/数据固定，失败输出阶段时间，避免用单元测试墙钟偶然性替代验收。

## Affected Surfaces

- `agent_harness.models`/`embeddings` 的 DTO、provider/router seam 和 adapters。
- CanonicalEvent payload contract、EventBus sinks、TelemetryFacade 与 service-app runtime composition。
- local JSONL 与现有 PostgreSQL canonical event storage；优先复用现有 JSON payload，不预设新 migration。
- contract/integration/eval/smoke 证据；不新增 HTTP endpoint。

## Testing Seams

- fake model、Pydantic AI adapter 替身与 OpenAI-compatible embedding 替身输出同一 DTO。
- reported/estimated/unavailable cost、token unavailable/真实零值、fallback、budget/policy required、timeout/provider failure。
- 同一次调用的 started/terminal evidence 逐值核对 tenant/run/request/agent/trace，且 terminal usage 恰好一条。
- local sink 先于 provider fan-out；provider failure 后仍可读取 local evidence。
- secret/prompt/embedding/raw response fixtures 不进入 DTO、event、trace、error 或 provider payload。
- public fake run smoke 在 5 秒内完成并输出可重复定位的阶段时延。

## Risks / Trade-offs

- [Risk] provider token 命名和 cost 数据差异大 → adapter 只映射可证明字段，未知值显式 unavailable，不猜测。
- [Risk] local write failure 与 provider 已产生调用成本之间出现不确定性 → started correlation 在调用前建立，失败返回封闭状态并保留可用的安全补偿证据，不自动重试非幂等 provider 调用。
- [Risk] 每次调用增加 event 数量 → payload 保持有界摘要，大内容用 ref；P0 接受可审计性带来的固定开销。
- [Risk] 5 秒阈值受机器负载影响 → fake/local fixture 无网络依赖，记录分阶段时延并允许 CI 基于明确环境重跑，但不得放宽产品门禁。

## Migration Plan

先引入 DTO 与 red contract tests，再让 fake/model/embedding adapters 产出 evidence，随后接入 EventBus/TelemetryFacade 和 service runtime，最后启用 local latency smoke。旧 `ModelResponse.token_usage` 可在内部适配期读取，但公共 evidence 必须由统一 DTO 生成；迁移完成后禁止业务 agent 拼 raw usage。本 change 不自动归档。

## Open Questions

无。Delegation aggregation 与 SSE 已由后续相关 change 承担，并继续受 DEV-PLAN 串行 DAG 与联合审查约束。
