## Source Links

- Product-Spec.md: SCOPE-005、SCOPE-007、TASK-005、REQ-006 与 REQ-022 的 service profile、durable runtime 和未来拆分边界。
- DEV-PLAN.md: Phase 13 的 Redis queue message、worker pickup、重试幂等与跨进程关联字段验收。
- Design-Brief.md or design artifact: 不涉及 UI；部署边界图只作为后续 `service-profile-deployment-proof` 的输入。
- CONTEXT.md / ADR: 当前仓库无 CONTEXT/ADR；本变更不得替代 Phase 13 后续 ADR。

## Why

现有 service profile 只检查 Redis 可达性，API 与 worker 之间没有可消费、可确认、可恢复的 run message。必须先建立 provider-neutral queue seam 和 Redis Streams 交付语义，后续分进程 runtime 才有可审查的幂等与故障恢复基础。

## What Changes

- 定义稳定 `RunQueueMessage`、带 consumer/delivery fencing 的 receipt、`RunQueue` protocol 和 queue error seam；message kind 覆盖初始执行与审批 continuation refs。
- Redis Streams adapter 使用 consumer group 完成 enqueue、阻塞 pickup、ack 与超时 pending reclaim。
- message header 强制携带每个逻辑 operation 首次提交固定的 `request_id`、`operation_id`、effective `idempotency_key`、`tenant_id`、`run_id` 和 `schema_version=1`；payload 只含执行所需的稳定 refs。
- 初始执行与每个 approval continuation 使用不同 `operation_id`；同一 operation 重放稳定、不同 operation 不冲突。初始执行未传客户端 key时使用 operation id，传 key时保留原值；approval continuation使用 operation id作为 effective key。
- 增加 fake queue 与真实 Redis 合同，覆盖正常交付、重复 enqueue、崩溃后 reclaim、ack 后不再投递和非法 payload fail closed。

## Non-Goals

- 不修改 HTTP route 的同步/异步行为，不启动 API/worker 独立进程。
- 不在本变更内执行 `RunOrchestrator`、DBOS workflow 或业务 agent。
- 不增加死信管理 UI、完整 broker 管理面、Phase 14 深度文档或 Phase 15 发布自动化。

## Capabilities

### New Capabilities

- `durable-run-queue`: 定义 run queue DTO、Redis Streams consumer-group 交付、ack/reclaim、幂等 enqueue 和关联字段边界。

### Modified Capabilities

- 无。

## Impact

- 核心包新增 `agent_harness.runtime.queue` 与 `agent_harness.adapters.queue.redis`，并从公共边界导出 DTO/protocol。
- 核心依赖新增 `redis==8.0.1` Python client；Phase 13 Compose 与真实合同统一固定 Redis 8.0.1。
- 测试新增 fake/contract 与真实 Redis 条件集成证据；不修改数据库 schema、HTTP API 或模板进程模型。
