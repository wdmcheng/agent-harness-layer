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

### Requirement: Runtime 可分离 queued run 提交与既有 run 执行
`RunOrchestrator` SHALL提供 provider-neutral seam，把 run持久化为现有 `created`状态，并在同一事务保存脱敏 `IdentityContext` snapshot、request/trace correlation、queue operation/effective key/首次 request id与 `enqueue_pending`。只有 Redis幂等接受 message、repository记录 `queued`/message id后才能以稳定 event id发布 `run.queued` 并返回成功；service profile的 RUN-001 queued成功 MUST返回 HTTP 202，local profile继续 inline执行并沿用 HTTP 200。执行 seam MUST从 storage读取权威 run/context，不得由 queue payload或 profile default重建第二 run。

#### Scenario: Service API 提交 queued run
- **WHEN** service profile 的 RUN-001 通过认证、policy 和 guardrail 后提交 run
- **THEN** runtime持久化唯一 `status=created` run和 queue/execution context，Redis接受后写 queued/message ref、发布唯一 `run.queued`并以 HTTP 202返回 `created` DTO；executor不在 API进程执行

#### Scenario: Local API 继续同步执行
- **WHEN** local profile的 RUN-001通过既有 inline runtime执行
- **THEN** HTTP继续返回 200与既有执行结果语义，不创建 Redis message或 DBOS workflow

#### Scenario: RUN-001 enqueue 失败保留可补投状态
- **WHEN** run与 `enqueue_pending`已提交但 Redis enqueue、queued状态更新或 `run.queued` evidence任一步失败
- **THEN** API返回 503 `run.enqueue_unavailable` `ApiErrorEnvelope`且不声称已 queued；同客户端 key重试复用原 run/operation，worker startup/pickup recovery也可幂等对账 Redis并补齐 queued/message/event，首次 request id不被新 attempt覆盖

#### Scenario: 无客户端 key 的 pending run 也不会 orphan
- **WHEN** 未带客户端 idempotency key的 RUN-001在创建唯一 run后 enqueue失败，调用方可能发起新的非幂等请求
- **THEN** 原 run仍由保存的 operation/effective key和 worker recovery补投；新请求可按既有 HTTP语义创建另一 run，但原 created run不得永久遗留或被改写为新请求的 run

#### Scenario: Worker 执行同一既有 run
- **WHEN** worker 用 queue message 的 tenant/run 调用执行 seam
- **THEN** runtime 校验 tenant 与 execution identity snapshot，使用稳定 DBOS owner 原子把该 run 从 created 推进到 running，调用已注册 executor，并把 checkpoint、output 和 CanonicalEvent 写回同一 run

#### Scenario: 重复 pickup 不产生第二个终态
- **WHEN** 同一 message 因重试或 reclaim 再次请求执行已 running、waiting 或 terminal run
- **THEN** runtime 不创建新 run；terminal/waiting run 返回稳定已有结果，同一 DBOS owner 可恢复 running run，不同 owner fail closed 且不得调用 executor

#### Scenario: 持久化身份与请求字段跨进程保持一致
- **WHEN** API actor 具有非默认 roles/permissions/auth_method、input 带 source/trust/context refs，并提交 service run
- **THEN** worker 从脱敏 execution context 与持久化 run/input重建同一 actor/correlation，policy/tool/context/event/audit 使用原值；profile default identity、queue payload或新 request id不得覆盖

### Requirement: DBOS service adapter 提供稳定 workflow 幂等边界
service worker SHALL只通过 `DBOSRuntimeAdapter`使用 DBOS 2.26.0，并为每个 queue operation派生 workflow id。当前 singleton service profile只允许一个 active worker，显式使用稳定 `executor_id=agent-harness-service-worker`；替代进程仅在前任完全退出后复用该 id恢复其 PENDING workflows。并行 replicas共享该 id MUST fail closed；当前 singleton service profile不支持并行 worker pool，后者需要 Conductor或独立契约。初始 execute workflow是 application run execution owner，owner/ref存入 run；每个 approval lease使用独立 workflow id，ref存入 resolution state并受 lease fencing。每个 DBOS workflow MUST用 durable step调用匹配 operation kind的 provider-neutral handler。初始重入先读 run：terminal/waiting直接复用，running同 execute owner才恢复，created同 owner才 claim；approval重入先读 resolution/claim，completed返回已有结果，当前 lease才恢复。DBOS类型 MUST留在 adapter；core runtime、HTTP DTO、CanonicalEvent和业务 agent不得 import或序列化 DBOS object。

#### Scenario: 同一 operation 重放复用 DBOS workflow
- **WHEN** Redis重投同一 execute/approval operation或 worker重启后再次请求 DBOS执行
- **THEN** adapter使用该 operation相同 workflow id取得或恢复同一 execution；同一 run的不同 approval lease使用不同 workflow且不与初始 execute workflow冲突

#### Scenario: DBOS 中断后从持久化状态恢复
- **WHEN** worker 在 DBOS workflow 执行期间退出，随后以同一 PostgreSQL system database 和 run message 重启
- **THEN** 前任进程先完全退出，替代 worker使用相同稳定 executor id；DBOS恢复分配给该 executor的同 workflow/durable step，handler以同 operation owner重新进入并先读共享状态，最终只产生一个 terminal event

#### Scenario: P0 拒绝并行 worker 复用 executor id
- **WHEN** service配置尝试同时启动两个使用 `agent-harness-service-worker` 的 active worker replica
- **THEN** readiness/config gate拒绝第二实例并给出需 Conductor/worker-pool设计的诊断，不允许两个进程并发声称同 executor ownership

#### Scenario: Vendor 对象不越过 adapter
- **WHEN** boundary test 扫描 core runtime、service API、worker message DTO 与业务 agent
- **THEN** 只有受控 `adapters/runtime/dbos.py` 可 import DBOS，其他边界只出现 provider-neutral DTO/protocol

#### Scenario: DBOS 状态映射不会过早 ack
- **WHEN** DBOS workflow 状态为可恢复中断、adapter/system database 不可用或结果不确定
- **THEN** worker 不把 run伪造为 failed/completed且不 ack queue；只有 DBOS `ERROR`/`MAX_RECOVERY_ATTEMPTS_EXCEEDED` 被 adapter确定分类、application failed terminal成功持久化后才允许 ack

### Requirement: Service CanonicalEvent 使用 PostgreSQL 跨进程 sink
service profile SHALL 使用 `PostgreSQLEventSink` 通过现有 `canonical_events` 表保存完整 CanonicalEvent envelope。sink MUST 对同一 run 的 seq 分配进行数据库级串行化，并以 event id 幂等、`(run_id, seq)` 唯一和每 run 唯一 terminal 约束拒绝跨进程竞态；local profile 继续使用 JSONL。

#### Scenario: API 与 worker 共用有序 event stream
- **WHEN** API 发布 `run.queued` 后独立 worker发布 started/checkpoint/terminal events
- **THEN** PostgreSQL sink返回同一 run从 1 单调递增的完整 envelope，HTTP events seam可读且保留 request/trace/user/source/trust refs

#### Scenario: 并发 terminal 写只有一个成功
- **WHEN** DBOS recovery与旧 worker竞态写同一 run的 terminal evidence
- **THEN** 数据库唯一约束只允许一个 terminal；幂等 event id返回已有 event，非幂等第二终态返回稳定 conflict且不消耗额外 seq

### Requirement: Service approve continuation 由 worker 执行，deny 在 API 原子收口
service profile的 `APR-002 decision=approve` SHALL在 API进程完成认证/policy、原子取得 resolution lease后，持久化 lease、operation id、首次 request id、`resolution_state=claimed`与 enqueue状态，再投递独立 `resume_approval` operation；worker MUST从 approval/resolution/run execution context重建匹配的 `ApprovalGrant`，并在启动该 lease专属 DBOS workflow前以 CAS把 resolution state从 `claimed`迁移为 `execution_owned`、持久化 workflow owner/ref，再通过相同 provider-neutral runtime resume seam恢复原 executor/tool continuation。`decision=deny` MUST沿用 repository条件更新原子写 denied/event/audit并使目标动作不执行，不创建 resolution lease、queue operation或 DBOS workflow。API进程不得为 approve调用 executor/tool，也不得把 deny发送给 worker。

#### Scenario: Approval resolve 排队后由 worker恢复
- **WHEN** executor-produced approval处于 waiting，reviewer通过 APR-002 approve
- **THEN** API 返回 resolution queued/in-progress语义并投递 approval refs，worker验证 tenant/identity/agent/run/action/resource/arguments hash/lease后恢复同一 continuation，handler恰好一次且 terminal唯一

#### Scenario: Deny 原子终止且零 continuation message
- **WHEN** reviewer在 service profile对 waiting approval提交 deny
- **THEN** API/repository原子写 denied与唯一 event/audit，run进入既有 failed/fallback语义；不创建 resolution lease、operation/message/DBOS workflow，executor/tool handler计数为零

#### Scenario: Approve 与 deny 并发只有一个决策胜出
- **WHEN** approve与 deny并发提交同一 waiting approval
- **THEN** repository条件仲裁只允许一个终态；deny胜出则零 queue，approve胜出则只有一个 lease/operation，失败方返回稳定 409且不产生第二个 audit/handler

#### Scenario: Approval continuation 重启与旧 lease fail closed
- **WHEN** worker在 approval resume中断、message被 reclaim或旧 lease/message重复到达
- **THEN** 新 worker以当前 resolution lease和同 DBOS owner恢复；过期/不匹配 lease不得调用 handler，已完成 claim返回已持久化结果且不产生第二个 audit/terminal

### Requirement: Approval enqueue 失败可按同 lease 幂等补投
approval repository SHALL在 claim approve lease的同一事务持久化 `resolution_operation_id`、首次 `resolution_request_id`、`resolution_reviewer_id`、`resolution_decision`、规范化 `resolution_request_hash`、`resolution_state=claimed`和 `enqueue_state=enqueue_pending`。这些 fingerprint/state字段是私有列/DTO，不得放入 public metadata/ApprovalRecord。Redis enqueue成功后 SHALL写 `enqueue_state=queued`与 message id。enqueue失败 MUST返回可重试的 `approval.enqueue_unavailable`。仅当 active lease仍为 pre-execution `resolution_state=claimed`、处于 `enqueue_pending|queued`、尚无 tool claim且 reviewer/decision/规范化 request hash全部相同时，APR-002重试才能复用原 operation：`enqueue_pending`幂等补投，`queued`复用既有 message/ref并只补齐 evidence。worker启动恢复器也 MUST只处理保存了完整 fingerprint、尚无 tool claim的 active `claimed+enqueue_pending` lease；不得换 lease或用新 attempt id覆盖。worker pickup在创建 DBOS workflow前 MUST以 CAS迁移为 `execution_owned`并保存 workflow owner/ref；此后只有同 owner恢复，或由 fingerprint匹配的真实 APR-002请求在 lease超时且无 claim时，于同一事务审计旧 operation/message/workflow refs、换发新 resolution lease id、按新 id派生 operation、以本次 request id建立新 operation首次 correlation、重新绑定已验证 fingerprint、清空 active message/workflow refs并重置为 `claimed+enqueue_pending`。不同 fingerprint、已有 claim或其他私有 state仍按既有409/恢复边界 fail closed。

#### Scenario: Lease 落库后 Redis 失败由 API 重试补投
- **WHEN** approve active lease/operation处于 `resolution_state=claimed`与 `enqueue_pending|queued`、尚无 tool claim，且调用方以相同 reviewer/decision/规范化 request body重试 APR-002
- **THEN** API复用原 lease、operation与首次 request id；`enqueue_pending`幂等 enqueue原 message并更新 queued/message ref，`queued`复用既有 message/ref且不创建第二 message；不返回泛化 resolution-in-progress 409，不创建第二 lease

#### Scenario: Worker 启动恢复 pending approval enqueue
- **WHEN** API未重试且 worker启动时发现同 tenant的 active `resolution_state=claimed`、`enqueue_state=enqueue_pending` approve lease，私有 reviewer/decision/规范化 request hash完整且尚无 tool claim
- **THEN** 恢复器通过公开 repository/queue seam幂等补投存储的 operation并标记 queued；fingerprint不完整、已有 claim、旧/过期/deny/terminal或其他私有 state不被投递

### Requirement: Run enqueue 失败可按 operation 幂等恢复
run repository SHALL私有持久化 `queue_operation_id`、`queue_request_id`、`queue_effective_idempotency_key`、`queue_enqueue_state=enqueue_pending|queued`与 `queue_message_id`。RUN-001同客户端 idempotency key重试、worker启动扫描与 worker pickup对账 MUST复用这些字段幂等补投/确认 message；只有 state queued且稳定 `run.queued:<run_id>` evidence已写才算提交成功。私有 queue字段不得进入 public run DTO。

#### Scenario: Worker startup 恢复 pending run enqueue
- **WHEN** service worker启动时查询到 `status=created`且 `queue_enqueue_state=enqueue_pending`的 run
- **THEN** worker使用保存的 operation/effective key/首次 request id幂等 enqueue，对账 stream id，原子标记 queued并发布稳定 `run.queued` event；不得调用 executor直到对账完成

#### Scenario: Pickup 对账 API 中断窗口
- **WHEN** Redis已接受 message但 API在保存 queued/message ref或发布 evidence前中断，worker先 pickup该 entry
- **THEN** worker验证 message与 run私有 fingerprint一致，补齐 queued/message ref和唯一 `run.queued`后才执行；不创建第二 run/message/event
