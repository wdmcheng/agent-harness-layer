## Source Links

- Product-Spec.md: SCOPE-003、SCOPE-005、SCOPE-007、REQ-006、REQ-022，及 service profile 支持后端服务化部署的成功标准。
- API-Contract.md: RUN-001、runtime worker 与 future API/worker split 的调用方映射和关联字段约束。
- DEV-PLAN.md: Phase 13 的独立 API/worker、DBOS service adapter、shared checkpoint、event stream 与 worker pickup 验收。
- Design-Brief.md or design artifact: 不涉及 UI；沿用现有运行链路与信任边界图的 DTO/CanonicalEvent 边界。
- CONTEXT.md / ADR: 当前仓库无 CONTEXT/ADR；部署决策由下游 change 固化。

## Why

当前 API 直接执行 run，worker 的 `--once` 也只是自行创建另一条 run，不能证明 API 提交的任务由独立进程接手。Phase 13 需要在不破坏 local inline 行为的前提下，把 service profile 的提交与执行拆开，并让 DBOS、checkpoint 和事件都穿过真实共享边界。

## What Changes

- 为 `RunOrchestrator`增加“持久化 `created` run + 私有 enqueue_pending refs”“Redis接受后发布 `run.queued`”与“执行既有 run”的 seam；local继续 inline。
- service RUN-001持久化 execution identity/correlation与 queue operation/fingerprint，再投递 message；失败返回可恢复 503，API同 key重试或 worker startup/pickup recovery补投，成功才返回 `created`。
- runtime worker 消费 queue message，从 PostgreSQL 的 run execution context 重建原 identity/input，调用同一 orchestrator executor seam并写共享 checkpoint/PostgreSQL event stream。
- service profile 的 APR-002 `approve` 只 claim resolution lease、持久化 enqueue operation状态并投递 `resume_approval` refs；worker重建 `ApprovalGrant`并恢复。`deny`继续在 API/repository原子收口，绝不创建 lease/message或执行 handler。
- 实现受控 DBOS 2.26.0 adapter，以 tenant/operation派生 workflow id；P0单 worker使用稳定 `executor_id=agent-harness-service-worker`，A完全退出后 B复用该 id自动恢复归属 workflow。initial owner/ref写 run，approval owner/ref写 resolution。
- 失败时只在执行已确定失败后 ack；worker 崩溃或不确定执行保留 pending，供 reclaim 后通过同一 run/workflow 恢复。

## Non-Goals

- 不拆 tool/model gateway、storage service 或 observability/event pipeline。
- 不新增远程 tool route、SSE/WS endpoint 或业务 agent 行为。
- 不在本变更编排 Docker Compose 或完成部署图/ADR；这些由依赖本变更的部署证明 change 负责。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `runtime-checkpoint-runs`: 增加 queued run 提交、既有 run 执行、DBOS workflow idempotency 与跨进程 checkpoint/event 恢复要求。
- `service-app-shell`: service profile API/worker 必须通过公共 queue/runtime seam 分离提交与执行，同时保留 local profile inline 行为。
- `auth-policy-hitl-approvals`: 为 service profile approve enqueue 增加 active lease 的窄幂等补投语义；deny与其他冲突继续沿用既有 409/原子仲裁。

## Impact

- 修改 runtime orchestrator/repository/event sink 公共 seam、service-app runtime composition、RUN-001/APR-002 adapter 与 runtime worker。
- 增加 DBOS 2.26.0 受控 adapter 依赖及测试；vendor 类型不得进入 core DTO、API 或业务 agent。
- 新增 `0012_service_runtime_execution_context` migration，持久化 run/approval私有 queue operation、request/fingerprint、enqueue/message/workflow refs、execution context和完整 event envelope；API-Contract同步 service RUN-001的202、local RUN-001的200，以及 APR-002的202/409/503语义。
