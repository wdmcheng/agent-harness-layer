## Source Links

- Product-Spec.md：`REQ-012` 模型、预算、上下文组装与 embedding，`AC-064`；`REQ-019` 非功能验收 `AC-065`。
- DEV-PLAN.md：`Phase 13.7: Model / Embedding Usage Evidence 与 Local Latency`；依赖 `Phase 13.6` 与 `Phase 13.6A run-trace-correlation`，并为 `Phase 13.8` 的 parent usage aggregation 提供 durable evidence。
- API-Contract.md：`MOD-001` `ModelUsageEvidence` provider-neutral schema 及关联字段。
- 设计稿 / 架构图：`docs/architecture/pydantic-ai-agent-architecture.drawio` 的 model adapter、EventBus、TelemetryFacade 与 local-first trace/eval 闭环；`docs/architecture/agent-harness-runtime-trust-boundaries.drawio` 的 tenant/identity/run/trace 信任边界。
- ADR：`docs/adr/0001-p0-service-boundaries.md` 的 credential、trace、artifact 与 service composition 安全边界。

## Why

当前模型路由、预算判断和 observability seam 已存在，但 model/embedding 调用尚未产生一份可按 tenant、run、request、agent 和 trace 关联的 provider-neutral usage evidence。缺少该证据会让成本、时延、失败和后续 delegation 聚合只能依赖 provider 原始事件或业务 agent 手工拼接，不能作为可信 P0 基线。

## What Changes

- 实现 API Contract 已定义的统一 `ModelUsageEvidence`，固定 `usage_kind`、tenant/provider/model、nullable token、`cost_usd`/`cost_status`、latency、decision 与 run/agent/request/trace 关联，不另造第二套 DTO。
- 由 adapter/router 边界归一化 provider usage；业务 agent 不接触 provider SDK 对象或拼接 raw usage。
- 通过 CanonicalEvent/EventBus 与 TelemetryFacade 先持久化 local evidence，再可选 fan-out 到 provider；provider 失败不得删除或隐藏本地证据。
- model request started、usage updated 和失败 evidence 使用同一 tenant/run/request/agent/trace 关联；失败路径保留有界、脱敏、可结算摘要。
- local fake provider smoke 记录入口到 terminal 的总时延，并以稳定的 5 秒门禁验证 `AC-065`。

## Non-Goals

- 不实现真实 agent delegation、parent/child usage aggregation 或 delegation API；这些属于 `agent-delegation-execution`。
- 不实现 SSE transport、`Last-Event-ID` 或首 frame 门禁；这些属于 `sse-event-streaming`。
- 不上传 prompt、embedding 原文、provider raw response、provider client 或 secret；不要求真实 provider 凭据或 SaaS telemetry 服务。
- 不自动归档、发布、push 或推进 Phase 14/15。

## Capabilities

### New Capabilities

- `model-usage-evidence`：定义 model/embedding provider-neutral usage、稳定关联、local-first 持久化、失败与脱敏语义及 local latency 门禁。

### Modified Capabilities

- `agent-registry-model-context`：把模型路由与预算决策的结果关联到统一 durable usage evidence，而不是 provider 原始事件。
- `observability-provider-adapters`：明确 usage evidence 先写 local，再进行脱敏 provider fan-out，并保持 provider failure degraded 语义。
- `canonical-events-artifacts`：增加 model/embedding usage canonical event 的稳定关联和 payload 边界。

## Impact

- 受影响代码：`agent_harness.models`、`agent_harness.embeddings`、model/embedding adapters、`agent_harness.events`、`agent_harness.observability` 和 service-app runtime composition。
- 受影响数据：沿用 canonical events/local telemetry 与 trace refs，不在本 change 引入 delegation 聚合表或公开 provider raw payload。
- 受影响测试：model/context、embedding、event、observability contracts，以及 `scripts/smoke_local.py` 的稳定入口时延证据。
- 不要求真实 API key，不新增 SSE endpoint，不改变 Phase 14/15 状态。
