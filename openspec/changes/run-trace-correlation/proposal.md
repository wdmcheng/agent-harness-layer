## Source Links

- Product-Spec.md：FLOW-003 危险动作审批与恢复的 `tenant_id/agent_id/run_id/trace_id` 关联边界；REQ-014 CanonicalEvent；REQ-015 Observability；数据规则中全局唯一 `trace_id`。
- DEV-PLAN.md：Phase 13.6 与 Phase 13.7 之间的 P0 基线补缺；后续 model usage、delegation 与 SSE 必须复用同一 run trace。
- API-Contract.md：通用关联规则、5.15 `ApprovalRecord`、5.29 `ModelUsageEvidence` 和 RUN-001。
- 设计稿 / 架构图：`docs/architecture/agent-harness-runtime-trust-boundaries.drawio` 的 run/trace 信任边界；`docs/architecture/agent-harness-technical-architecture.drawio` 的 API、runtime、event、approval 与 observability 数据流。
- ADR：`docs/adr/0001-p0-service-boundaries.md` 的 API/worker DTO、credential 与 trace 关联边界。

## Why

Product Spec 要求 approval 与 run trace 强关联，但当前 API Contract、DTO 和持久化允许 `trace_id=null`；同时 `ModelUsageEvidence` 已把 `trace_id` 定义为必填。若不先建立统一的 run trace 生成与传播规则，approval、model usage、delegation 和 SSE 会形成互不兼容的关联语义，Phase 1-13 也不能作为可信审计基线。

## What Changes

- 为每个新 run 建立不可为空、全局唯一的 canonical `trace_id`：调用方提供合法值时保留，缺失时由受控 runtime composition 在任何持久化事件或 provider/tool 副作用前生成。
- 把 canonical `trace_id` 持久化到 run execution context，并传播到 checkpoint/resume、CanonicalEvent、approval/audit、model/embedding evidence、worker message 和后续 child delegation。
- 将 `ApprovalRecord.trace_id` 收紧为必填；API/CLI/body 不得覆盖 run 已绑定的 canonical trace。
- 为已有 nullable trace 数据提供确定性、幂等、可回滚的 migration/backfill；读取兼容只允许存在于迁移窗口，不得长期保留新记录写入 null 的双轨。
- 增加 local/service、restart/resume、approval 与跨进程 worker 的 trace 一致性合同，并把后续 `model-usage-evidence`、`agent-delegation-execution`、`sse-event-streaming` 明确设为相关下游 change。

## Non-Goals

- 不引入外部 tracing backend、provider SDK、采样策略、跨服务 baggage 标准或 trace 搜索 API。
- 不改变 approval 状态机、危险动作 policy、delegation 执行、ModelUsageEvidence 字段、SSE transport 或 Phase 14/15。
- 不把 `request_id` 与 `trace_id` 合并；request 仍表示单次入口请求，trace 表示一个 run 及其受控下游工作的长期关联。
- 不自动归档、发布、push 或标记 Phase 14/15 完成。

## Capabilities

### New Capabilities

- `run-trace-correlation`：定义 canonical run trace 的生成、持久化、传播、历史 backfill 和跨入口一致性。

### Modified Capabilities

- `runtime-checkpoint-runs`：run create/checkpoint/resume/worker execution 必须保留同一非空 trace。
- `auth-policy-hitl-approvals`：approval 与 audit 必须从 run 继承非空 trace，调用方不得覆盖。
- `canonical-events-artifacts`：同一 run 的 lifecycle、approval、tool/model 与 terminal event 必须携带 canonical trace。
- `service-app-shell`：RUN-001 接受可选 `X-Trace-Id`，缺失时由服务端生成并进入后续关联证据。

## Impact

- 受影响代码：runtime orchestration/execution context、approval service/repository、CanonicalEvent/EventBus、API/CLI composition、worker queue context 和 storage migration。
- 受影响 API：`ApprovalRecord.trace_id` 从 nullable 收紧为 required；RUN-001 的可选 trace header 与服务端生成语义被正式记录。
- 受影响数据：现有 nullable run execution context、approval、event、audit/trace refs 需要确定性 backfill；不得删除历史 evidence。
- 受影响测试：SQLite/PostgreSQL migration、API/CLI/local/service run、restart/resume、approval、worker 与 trace/evidence contract。
