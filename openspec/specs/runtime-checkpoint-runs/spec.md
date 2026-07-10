# runtime-checkpoint-runs Specification

## Purpose
定义 provider-neutral run lifecycle、idempotency、checkpoint/resume 与持久化事件边界的长期契约，确保 run 在重复提交、非法状态转换和进程重启场景下保持确定性，并让 API、CLI 和 worker 共用同一 runtime seam；service profile 的 DBOS 集成只能留在受控 adapter 边界，不得泄漏到 runtime core 或业务 agent。

## Requirements
### Requirement: RunOrchestrator 管理 run lifecycle
package SHALL 暴露 provider-neutral `RunOrchestrator`，负责创建、取消、恢复 run，并通过 repository/UoW 和 EventBus 记录 lifecycle。

#### Scenario: Fake agent run 产生 terminal event
- **WHEN** 调用方使用 fake agent 创建 run
- **THEN** run 进入 terminal status，并产生且只产生一个 terminal CanonicalEvent

#### Scenario: Run state transition 被校验
- **WHEN** 调用方尝试从 terminal status 继续执行或取消 run
- **THEN** runtime 拒绝非法 transition，并保留原 terminal status

### Requirement: RunOrchestrator 通过 AgentExecutor 执行业务 agent
runtime SHALL 定义 provider-neutral `AgentExecutor` request/context/result seam；`RunOrchestrator` MUST 在创建 run 和发布 started event 后调用由 registry 受控解析并注入的 executor，并根据 typed result 写入真实 output、failed terminal event 或 waiting checkpoint。模板内全部 agent（包括 basic/fake smoke agent）MUST 显式绑定 executor；runtime 不得保留无 executor 时固定返回 `fake-ok` 的隐式 fallback。

#### Scenario: 模板既有 Agent 显式迁移 executor
- **WHEN** executor reference 成为必填契约后加载 basic/fake smoke agent 或测试 fixture
- **THEN** config 显式声明并由 registry 解析 executor，run output 来自该 executor；缺失 executor 时启动/加载失败而不是产生固定成功 output

#### Scenario: Executor output 完成同一 run
- **WHEN** executor 返回 completed typed output
- **THEN** orchestrator 将该 output 写入同一 run，发布唯一 completed terminal event，CLI/API 返回该 run 的 terminal 摘要

#### Scenario: Executor 请求审批时进入 waiting
- **WHEN** executor 返回 `require_approval` result 和脱敏 approval request
- **THEN** orchestrator 为同一 run 写 checkpoint，并通过注入的 approval seam 创建关联 tenant/identity/run/trace/audit 的 approval record，不发布 terminal event

#### Scenario: Approval resume 重新进入原 executor continuation
- **WHEN** waiting run 的 approval 被原子 claim 并生成与 checkpoint 匹配的 `ApprovalGrant`
- **THEN** `resume_run` 解析持久化 continuation，重新调用原 agent executor 的 resume seam；只有 executor 返回真实 tool result 后才完成 run，不得直接写 `output={"resumed": true}` 伪造完成

#### Scenario: Executor failure 形成稳定失败终态
- **WHEN** executor 抛出受控执行错误或返回 failed result
- **THEN** orchestrator 写脱敏 error summary、发布唯一 failed terminal event，并保留 local trace，不泄漏 provider 原始对象或 secret

### Requirement: Idempotency key 防止重复 run
runtime SHALL 支持 idempotency key，使同一 tenant/agent/session 下的重复提交不会创建重复 run。

#### Scenario: 同一 idempotency key 返回同一 run
- **WHEN** 调用方用同一 idempotency key 重复创建 run
- **THEN** runtime 返回已有 run，而不是插入新的 run record

### Requirement: Checkpoint 支持进程重启后 resume
runtime SHALL 提供 checkpoint store 和 resume token，使普通 checkpointed run 可在新的 orchestrator instance 中恢复。approval-gated checkpoint MUST NOT 仅凭原始 resume token 通过公开 `RUN-005` 推进；公开请求 MUST 在消费 token、推进 run 或调用 tool handler 前返回稳定 `409 run.invalid_transition`。这类 checkpoint 只能由 `APR-002` 先取得私有 resolution lease、生成与 checkpoint 完整绑定的 `ApprovalGrant`，再调用 runtime 内部 resume seam。

#### Scenario: 重启后从普通 checkpoint resume
- **WHEN** 非 approval-gated 的 run 写入 checkpoint 后进程重启并重新构造 orchestrator
- **THEN** 调用方可以使用 resume token 恢复 run，后续事件 seq 继续递增

#### Scenario: 公开 resume 拒绝 approval-gated checkpoint
- **WHEN** 调用方把 approval-gated checkpoint 的原始 resume token 直接提交到公开 `POST /api/v1/runs/{run_id}/resume`
- **THEN** API 返回 `409 run.invalid_transition`，token 不被消费，run/approval 状态不变，tool handler 执行计数为零；token 即使匹配 tenant、identity、run 和 approval context 也不能替代审批授权

#### Scenario: Approval resolve 通过内部 resume 推进
- **WHEN** `APR-002` 对 waiting approval 原子取得私有 resolution lease，并生成与 tenant、identity、agent、run、action、resource 和 arguments hash 匹配的 `ApprovalGrant`
- **THEN** `ApprovalService` 调用 runtime 内部 resume seam 重新进入原 executor continuation；公开 `RUN-005` 不参与该执行链

#### Scenario: Waiting approval 在进程重启后恢复原 continuation
- **WHEN** approval-gated run 已持久化 waiting checkpoint 和 approval record，进程随后重启，并使用同一持久化 storage 重新构造 registry、executor resolver、`RunOrchestrator` 和 `ApprovalService` 后通过 `APR-002` approve
- **THEN** 新实例从持久化 checkpoint 取得同一 private lease，生成匹配 `ApprovalGrant`，通过内部 resume 调用原 executor/tool handler 恰好一次，持久化真实 completed 或确定性 failed result并发布唯一 terminal event；公开 resume token 不被提交或消费

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
