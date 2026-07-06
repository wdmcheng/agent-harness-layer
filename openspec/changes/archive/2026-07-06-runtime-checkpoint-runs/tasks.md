## 1. OpenSpec 与 runtime 测试基线

- [x] 1.1 运行 `openspec validate runtime-checkpoint-runs --type change --strict`，确认本 change artifact 可解析。
- [x] 1.2 新增 public seam tests，覆盖 fake run terminal event、非法 state transition、idempotency、checkpoint resume、CLI run、DBOS boundary。

## 2. Runtime core

- [x] 2.1 实现 run state、terminal status、`ResumeToken`、`IdempotencyKey`、`CheckpointStore`、`ApprovalWaitState` seam 和校验。
- [x] 2.2 实现 `RunOrchestrator`，通过 repository/UoW 和 EventBus 创建、取消、恢复 fake run。
- [x] 2.3 实现 checkpoint store，证明重启后可 resume 且 event seq 继续递增。

## 3. DBOS adapter boundary

- [x] 3.1 新增 `agent_harness.adapters.runtime.dbos` interface/no-op adapter，不让 DBOS 类型泄漏到 runtime core 或业务 agent。
- [x] 3.2 扩展 import boundary check，允许 DBOS 只存在于 adapter/integration path。

## 4. API、CLI、worker 与 smoke

- [x] 4.1 新增 `agent-harness run <agent_id>` CLI，输出 run id、terminal status 和 terminal event summary。
- [x] 4.2 新增 service-app FastAPI app factory、run create/detail/events/cancel/resume routes 和 runtime worker shell，共用 `RunOrchestrator`。
- [x] 4.3 新增 local/service smoke，覆盖 API/CLI fake run、checkpoint/resume 和 worker shell；运行 `make quality`、`make test`、`make smoke-local`、`make smoke-service`。

## 5. 验证证据

- [x] 5.1 Runtime contract tests 覆盖 public runtime DTO/Protocol seam、fake run terminal event、illegal transition、idempotency、checkpoint resume、resume token/run_id 归属校验、FastAPI OpenAPI route registration、API request/error envelope、event stream seam 和 worker run seam。
- [x] 5.2 CLI contract test 覆盖 `agent-harness run fake-agent --profile local`，输出 `completed` terminal status 和 `run.completed`。
- [x] 5.3 `make smoke-service` 覆盖 service profile 依赖、shared storage/queue 基础证据和 `worker_run`；物理多进程 worker 拆分仍留给后续 Phase。
