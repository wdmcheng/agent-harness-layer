## Source Links

- Product-Spec.md：`REQ-012` 模型、预算、上下文组装与 embedding，`AC-064`；P0 非功能验收 `AC-065`。
- DEV-PLAN.md：`Phase 13.7: Model / Embedding Usage Evidence 与 Local Latency`；依赖 `Phase 13.6` 与 `Phase 13.6A run-trace-correlation`，并为 `Phase 13.8` 的 parent usage aggregation 提供 durable evidence。
- API-Contract.md：`MOD-001` `ModelUsageEvidence` provider-neutral schema 及关联字段。
- 设计稿 / 架构图：`docs/architecture/pydantic-ai-agent-architecture.drawio` 的 model adapter、EventBus、TelemetryFacade 与 local-first trace/eval 闭环；`docs/architecture/agent-harness-runtime-trust-boundaries.drawio` 的 tenant/identity/run/trace 信任边界。
- ADR：`docs/adr/0001-p0-service-boundaries.md` 的 credential、trace、artifact 与 service composition 安全边界。

## Why

当前模型路由、预算判断和 observability seam 已存在，但 model/embedding 调用尚未产生一份可按 tenant、run、request、agent 和 trace 关联的 provider-neutral usage evidence。缺少该证据会让成本、时延、失败和后续 delegation 聚合只能依赖 provider 原始事件或业务 agent 手工拼接，不能作为可信 P0 基线。

## What Changes

- 实现 API Contract 已定义的统一 `ModelUsageEvidence`，固定 `usage_kind`、tenant/provider/model、nullable token、`cost_usd`/`cost_status`、latency、decision 与 run/agent/request/trace 关联；数值拒绝 bool、负数与非有限值，cost/status 组合必须一致，不另造第二套 DTO。
- 由 adapter/router 边界归一化 provider usage；业务 agent 不接触 provider SDK 对象或拼接 raw usage。
- 由 CanonicalEvent/EventBus 唯一持久化 canonical usage evidence，再把已持久化 event 交给 TelemetryFacade 做可选 provider fan-out；Facade 不二次写 local usage，provider 失败不得删除或隐藏本地证据。
- 在 `0014` 增加 durable evidence outbox/usage settlement 与 per-run event capacity reservation：run 预留 terminal，受信 typed registry 在 provider/tool/approval/delegation 副作用前按封闭 operation kind 预约最大 prerequisite event 数；容量基数固定为 `highest_persisted_seq` 而非 row count，预约消费、event 插入与 high-water mark 推进同事务；provider/tool 结果只持久化一次，sink 失败或确认丢失只按稳定 event id 补投，未知结果保持预约并阻止 terminal。
- 固定公共 canonical JSON serializer 及正常 CanonicalEvent envelope `65536` bytes 写入上限；大 payload 先 artifact 化，仍超限在持久化/fan-out 前拒绝，legacy/direct-write 超限 evidence fail closed。
- model request started、usage updated 和失败 evidence 使用同一 tenant/run/request/agent/trace 关联；失败路径保留有界、脱敏、可结算摘要。
- embedding cache hit 仍产生 started/final 调用级 evidence，但只记录本次 lookup latency、null token/cost 与 `cache_status=hit/provider_called=false`，不复用首次 provider latency或伪造 provider side effect。
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
- `canonical-events-artifacts`：增加 model/embedding usage canonical event 的稳定关联、payload 硬边界和副作用前 event capacity reservation。
- `auth-policy-hitl-approvals`：把 approval resolution 与 run terminal 纳入同一有序 evidence outbox，前置证据完成后才公开 resolution。
- `runtime-checkpoint-runs`：把 approve/deny 仲裁、continuation 与 terminal 收口切换为 outbox 前置证据优先恢复。
- `service-app-shell`：API 只提交仲裁/outbox 或 approve enqueue，worker 必须等待前置证据与 terminal 有序完成后才确认消息。
- `service-deployment-boundaries`：真实 PostgreSQL/Redis smoke 证明 approve/deny 有序证据、失败恢复与零副作用重放。
- `p0-example-agents`：示例危险工具 approve/deny 的公开状态、resolution 与 terminal 遵守同一有序证据语义。
- `storage-migration-uow`：固定 `0014` outbox/settlement/capacity upgrade、旧 run 预约 backfill，以及要求 evidence 全空和显式 `-x allow_empty_evidence_downgrade=true` 的 SQLite/PostgreSQL downgrade。

## Impact

- 受影响代码：`agent_harness.models`、`agent_harness.embeddings`、model/embedding adapters、`agent_harness.events`、`agent_harness.observability`、approval/runtime continuation、service-app API/worker 与示例 agent composition。
- 受影响数据：沿用 canonical events/local telemetry 与 trace refs，并以 `0014` 增加内部 `run_evidence_outbox`/usage settlement 和 per-run/operation event capacity reservation；不引入 delegation 聚合表或公开 provider raw payload。
- 受影响测试：model/context、embedding、event、observability、approval/runtime/service/example contracts，真实 service smoke，以及 `scripts/smoke_local.py` 的稳定入口时延证据。
- 不要求真实 API key，不新增 SSE endpoint，不改变 Phase 14/15 状态。
