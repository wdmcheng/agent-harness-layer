## 来源链接

- Product-Spec.md：`REQ-006` Durable runtime、checkpoint 和 resume；`REQ-008` API/CLI 与管理面；`REQ-014` CanonicalEvent 与流式输出；`AC-014`、`AC-060`。`AC-013` approval/HITL resume 只作为后续 Phase 7 背景，不由本 change 关闭。
- DEV-PLAN.md：`Phase 5: Durable Runtime、Checkpoint 与 Run 生命周期`；DBOS adapter 风险项；未来 service boundary 风险项。
- 设计稿 / 架构图：`artifacts/pydantic-ai-agent-architecture.drawio` 中 Agent Loop、Runtime、Checkpoint、Event Stream 和 worker boundary。
- CONTEXT.md / ADR：当前仓库无。

## 为什么

有了 storage 和 event spine 后，Phase 5 需要把 fake agent run 的创建、幂等、checkpoint、resume、terminal event 和 API/CLI 入口打通。DBOS 是 service profile 的 durable runtime 目标，但必须先隔离在 adapter interface 后面，避免业务 agent 直接依赖 DBOS API。

## 变更内容

- 新增 `RunOrchestrator`、run state machine、checkpoint store、resume token 和 idempotency key。
- 新增 local SQLite-backed checkpoint，并建立 `DBOSRuntimeAdapter` interface / boundary。
- 新增 fake agent runner，供 API、CLI 和 smoke 在无真实模型 key 时创建 run 并产出 terminal event。
- 新增 run API routes、`agent-harness run <agent_id>` CLI 和 runtime worker 壳。
- 新增 service smoke，证明 API/CLI fake run、worker/checkpoint/resume 和 shared storage/queue 配置可协作；若 Phase 5 尚未物理拆分 worker，必须给出同进程 worker 壳替代证据和 Phase 13 剩余边界说明。

## 非目标

- 不实现真实模型 provider、tool execution、approval UI、approval/HITL resume、完整 DBOS workflow 装饰器、物理多进程 worker 拆分或 Kubernetes 部署。
- 不绕过 Phase 3 repository/UoW 或 Phase 4 EventBus。

## 能力

### 新增能力

- `runtime-checkpoint-runs`：run lifecycle、checkpoint/resume、idempotency、DBOS adapter boundary、API/CLI run seam 和 worker shell。

### 修改能力

- `storage-migration-uow`：消费 run/checkpoint repository seam。
- `canonical-events-artifacts`：消费 EventBus、terminal event 和 event stream read seam。

## 影响

- 受影响代码：`packages/agent-harness/src/agent_harness/runtime/**`、`adapters/runtime/dbos.py`、`packages/agent-harness/src/agent_harness/cli.py`、`templates/service-app/app/api/routes/runs.py`、`templates/service-app/app/workers/runtime_worker.py`。
- 受影响测试：runtime contract tests、CLI/API smoke、service profile smoke。
- 受影响数据：`agent_runs`、`checkpoints` 和 `canonical_events` / local evidence。
