## ADDED Requirements

### Requirement: RunOrchestrator 管理 run lifecycle
package SHALL 暴露 provider-neutral `RunOrchestrator`，负责创建、取消、恢复 run，并通过 repository/UoW 和 EventBus 记录 lifecycle。

#### Scenario: Fake agent run 产生 terminal event
- **WHEN** 调用方使用 fake agent 创建 run
- **THEN** run 进入 terminal status，并产生且只产生一个 terminal CanonicalEvent

#### Scenario: Run state transition 被校验
- **WHEN** 调用方尝试从 terminal status 继续执行或取消 run
- **THEN** runtime 拒绝非法 transition，并保留原 terminal status

### Requirement: Idempotency key 防止重复 run
runtime SHALL 支持 idempotency key，使同一 tenant/agent/session 下的重复提交不会创建重复 run。

#### Scenario: 同一 idempotency key 返回同一 run
- **WHEN** 调用方用同一 idempotency key 重复创建 run
- **THEN** runtime 返回已有 run，而不是插入新的 run record

### Requirement: Checkpoint 支持进程重启后 resume
runtime SHALL 提供 checkpoint store 和 resume token，使 run checkpoint 后可在新的 orchestrator instance 中恢复。

#### Scenario: 重启后从 checkpoint resume
- **WHEN** run 写入 checkpoint 后进程重启并重新构造 orchestrator
- **THEN** 调用方可以使用 resume token 恢复 run，后续事件 seq 继续递增

### Requirement: API、CLI 和 worker shell 共用 runtime seam
service-app SHALL 暴露 run API route、`agent-harness run <agent_id>` CLI 和 runtime worker shell，它们都通过 `RunOrchestrator` 而不是直接操作 ORM session 或 DBOS API。

#### Scenario: CLI run 返回 terminal event
- **WHEN** developer 执行 `agent-harness run fake-agent --profile local`
- **THEN** command 输出 run id、terminal status 和 terminal event summary，并在无真实 model key 时成功

#### Scenario: API run route 创建 fake run
- **WHEN** service-app run API 创建 fake run
- **THEN** route 通过 `RunOrchestrator` 返回 public DTO，不暴露 ORM model、SQLAlchemy session 或 DBOS handle

#### Scenario: Service smoke 使用 service profile 依赖
- **WHEN** developer 执行 `make smoke-service`
- **THEN** smoke 启动本项目 PostgreSQL/Redis compose profile，执行 PostgreSQL migration、Redis reachability check，并通过 repository/UoW 写入 run 作为 service profile 证据

### Requirement: DBOS adapter 留在受控边界
service profile SHALL 只通过 `DBOSRuntimeAdapter` interface 接触 DBOS，业务 agent 和 runtime core model MUST NOT import DBOS directly。

#### Scenario: Static boundary check 阻止 DBOS 泄漏
- **WHEN** import boundary check 扫描 `agent_harness.runtime`、`templates/service-app/app/*` 和 `templates/service-app/agents/*`
- **THEN** DBOS import 只允许出现在 `agent_harness.adapters.runtime.dbos` 或明确批准的 integration path

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
