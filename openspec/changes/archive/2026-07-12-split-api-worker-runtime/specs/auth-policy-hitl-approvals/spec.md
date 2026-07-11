## MODIFIED Requirements

### Requirement: Approval API 和 CLI 管理审批生命周期
service-app SHALL提供 approval HTTP routes和 CLI，支持按 run list/read approval、approve、deny，并通过统一 `ApiErrorEnvelope`表达认证错误、权限错误、资源不存在和状态冲突。approve与 deny MUST通过同一 repository条件更新做原子 resolution仲裁：deny先赢时 public status变为 `denied`且目标动作不执行；approve lease先赢时 public status在动作得到持久化确定性结果并让 run进入 terminal前保持 `waiting`，并发 deny返回 `409 approval.resolution_in_progress`。local/raw `claimed` lease只有在 owner timeout到期且不存在 tool execution claim时才能由后续真实 API/CLI resolve重试原子接管并换发 resolution lease id；活跃 lease和已有 claim不得被抢占。service profile的 approve enqueue是 active-lease 409规则的窄例外：首次 approve在同一事务写 resolution lease id、稳定 operation/首次 request correlation、私有 reviewer/decision/规范化 request hash、`resolution_state=claimed`与 `enqueue_pending`；enqueue失败返回 503 `approval.enqueue_unavailable` `ApiErrorEnvelope`。仅当私有 resolution state仍为 pre-execution `claimed`、enqueue state为 `enqueue_pending|queued`、尚无 tool claim，且本次 reviewer、decision、规范化 request hash与私有 fingerprint全部相同时，APR-002重试 MUST复用原 lease/operation并可返回 202 waiting/queued摘要，不能换发 lease或暴露私有 fingerprint/enqueue state；API retry只补投 `enqueue_pending`，`queued`复用既有 message/ref且不得创建第二条 message。worker recovery也只补投 `resolution_state=claimed`、active `enqueue_pending`、无 tool claim且持久 fingerprint完整的 lease。worker在启动 DBOS workflow前 MUST以 CAS把 `resolution_state`从 `claimed`迁移为 `execution_owned`并持久化 workflow owner/ref；此后 API retry不得再走 enqueue窄例外。`execution_owned` lease过期且仍无 tool claim时，只有 fingerprint匹配的真实 APR-002重试可以 takeover：同一事务先把旧 operation/message/workflow refs写入审计 evidence，再换发新的 resolution lease id、按新 lease id派生新 operation、把本次真实 request id固化为该新 operation的首次 `resolution_request_id`、把已验证 reviewer/decision/规范化 request hash重新绑定新 lease、清空当前 active message/workflow refs，并回到 `claimed+enqueue_pending`；后续 retry不得覆盖新 operation的首次 request correlation，旧 operation/DBOS owner因 lease id不匹配 fail closed。不同 fingerprint、已有 tool claim、其他私有 state或并发 deny仍返回既有409；活跃 tool claim返回 `approval.resolution_in_progress`，执行结果不确定并已进入私有 `needs_review`的 claim返回 `approval.execution_needs_review`。动作无论 completed或确定性 failed，人工批准决定完成后 public status均变为 `approved`；只有执行结果不确定时才继续 `waiting`并进入不公开的私有 `needs_review`。基础设施 evidence失败 MUST保留私有 pending state，并由后续真实 API/CLI resolve重试在返回既有409前触发内部补偿。状态机 MUST防止重复 approve/deny、跨 run resolve、错误 resume token推进其他 run、旧 owner越过 fencing、重复工具副作用和第二个有效 resolution event/audit。

#### Scenario: 列出等待审批项
- **WHEN** run因危险动作进入等待审批状态
- **THEN** `GET /api/v1/runs/{run_id}/approvals`和 `agent-harness approvals list`返回 approval id、public status、action、resource、reason、tenant/agent/run/trace摘要和 `request_id`，不公开 private lease、enqueue或 internal state

#### Scenario: Approve 赢得仲裁后恢复原 run
- **WHEN** 已认证审批人对 waiting approval调用 approve且 repository条件更新先取得 private resolution lease
- **THEN** public approval继续保持 waiting；local模式直接通过 runtime resume，service模式返回 202 waiting/queued并由 worker恢复；并发 deny返回 `409 approval.resolution_in_progress`且不得改写 public status或发布第二个 resolution event

#### Scenario: 已批准动作以确定性结果完成 resolution
- **WHEN** approved continuation恰好执行一次，并持久化 completed或确定性 failed result，使原 run进入对应唯一 terminal
- **THEN** approval public status变为 approved，private lease被封存，audit log记录审批人、动作、真实结果和 trace，且只产生一次有效 approved resolution event/audit；确定性 failed不进入 needs_review

#### Scenario: 不确定执行结果保持等待人工复核
- **WHEN** tool execution claim已进入 executing，但进程在持久化 completed/failed result前中断
- **THEN** private resolution state变为 needs_review，public approval保持 waiting，不自动重放 handler，也不伪造 approved resolution

#### Scenario: Deny 赢得仲裁后失败或 fallback
- **WHEN** 已认证审批人先对 waiting approval调用 deny
- **THEN** approval状态变为 denied，后续 approve无法取得 lease并返回 `409 approval.invalid_transition`，目标危险动作不执行，run按 policy配置进入 failed或 fallback，并只写一次 denied resolution/audit evidence；service queue/DBOS operation为零

#### Scenario: 重复 public resolve 返回状态冲突
- **WHEN** 调用方对已 approved或 denied的 approval再次 approve/deny
- **THEN** API返回 409 `ApiErrorEnvelope`，CLI非零退出，且 run、tool handler、有效 resolution event和 audit log不被重复改写；若私有 state标记为 evidence pending，本次真实调用先触发内部幂等补偿再返回原 409，但不得变成 public幂等成功

#### Scenario: Service approve enqueue 失败可重试
- **WHEN** service approve lease与 enqueue_pending已提交但 Redis不可用
- **THEN** 首次调用返回 503 `approval.enqueue_unavailable`；相同 reviewer/decision/request hash重试复用原 lease/operation/首次 request id并在成功时返回 202 waiting/queued，public `ApprovalRecord.status`仍为 waiting且不包含私有 enqueue字段

### Requirement: Approval resume 使用私有 resolution lease 协调执行
approval repository SHALL为 waiting approval提供不进入 public `ApprovalRecord`/OpenAPI的私有 resolution lease。lease id本身是 fencing identity，MUST具有可配置 owner timeout且每次合法接管都换发新的 resolution lease id。service approve SHALL在 lease事务内额外保存稳定 operation id、首次 request id、reviewer id、decision、规范化 request hash、`enqueue_pending|queued`、`resolution_state=claimed|execution_owned`与 message/workflow refs；这些字段不得进入 public status/DTO或普通 metadata。pre-execution `claimed`与 `execution_owned` MUST通过 repository CAS互斥：前者才允许 enqueue retry/recovery，后者表示 DBOS owner/ref已耐久持久化且只能由同 owner恢复或在超时无 tool claim时换发新 lease/operation接管。未过期 lease和已有 claim不得被抢占。public status MUST在已批准动作得到持久化确定性结果并让 run进入 terminal前保持 `waiting`；结果无论 completed或确定性 failed，人工批准决定完成后均变为 `approved`，deny后变为 `denied`。只有执行结果不确定时 public status才继续 `waiting`并进入私有 `needs_review`；terminal/resolution evidence基础设施失败只能进入可补偿私有 pending state。不得把内部 `claimed`、`execution_owned`、`enqueue_pending`、`queued`、`recovery_pending`、`denied_pending`、`executing`或 `needs_review`暴露为新的 public status。

#### Scenario: Approve 原子取得私有 lease
- **WHEN** 第一个 approve请求处理 public status为 waiting且没有有效 resolution lease的 approval
- **THEN** repository以条件更新写 private lease id、claimed timestamp和 internal state；service模式同事务写 operation/首次 request/reviewer/decision/request hash/`enqueue_pending`，public status仍为 waiting，并发请求不能取得第二 lease

#### Scenario: Deny 先赢得 resolution 仲裁
- **WHEN** deny先以 repository条件更新把 waiting approval解析为 denied，随后 approve请求到达
- **THEN** approve不能取得 private lease，返回稳定 `409 approval.invalid_transition`，tool handler执行计数保持零，且只记录一次有效 denied resolution event/audit

#### Scenario: Approve lease 先赢得 resolution 仲裁
- **WHEN** approve先以 repository条件更新取得 private lease，随后并发 deny请求到达
- **THEN** deny返回稳定 `409 approval.resolution_in_progress`，public status在 approved tool execution完成前仍为 waiting，deny不修改 public status、不创建 tool claim，且同一 approval最终最多产生一次有效 resolution event/audit

#### Scenario: 进程硬退出后的过期 lease 可安全接管
- **WHEN** 进程在 private resolution lease已提交、但对应 `tool_invocations.approval_id` claim尚未创建时硬退出，owner timeout到期且真实 APR-002重试到达
- **THEN** 若 service lease仍为 `resolution_state=claimed`、`enqueue_pending|queued`、尚无 tool claim且本次调用的 reviewer/decision/规范化 request hash与私有 fingerprint全部相同，调用方复用原 operation而不换 lease；`enqueue_pending`幂等补投，`queued`复用既有 message/ref且不重复投递；若 lease已进入 `execution_owned`且过期、无 tool claim，repository仅接受 fingerprint匹配的真实 APR-002请求，并原子审计旧 refs、换发新的 resolution lease id、按新 id派生 operation、把本次 request id固化为新 operation首次 correlation、重新绑定已验证 fingerprint、清空 active message/workflow refs并写回 `claimed+enqueue_pending`，旧 operation/owner不得创建 tool claim

#### Scenario: 活跃 lease 不可被并发重试抢占
- **WHEN** private lease未超时或已有 tool claim，另一个 APR-002请求到达
- **THEN** repository不得换发 lease；只有 service `resolution_state=claimed`、`enqueue_pending|queued`、尚无 tool claim且 reviewer/decision/规范化 request hash完全相同的 retry可复用原 operation并返回 202/503恢复语义；`execution_owned`未过期或同 owner可恢复时返回 `409 approval.resolution_in_progress`，活跃 tool claim同样返回该错误，私有 state为 `needs_review`时返回 `409 approval.execution_needs_review`，其他 fingerprint或已收口状态沿用既有确定性409且不得重放 handler

#### Scenario: Lease fencing 与 tool claim 在同一事务生效
- **WHEN** owner准备在 handler前创建唯一 `tool_invocations.approval_id` claim
- **THEN** worker已先以 CAS把 `claimed`迁移为 `execution_owned`并保存当前 workflow owner/ref；repository MUST在同一 UoW内按当前 resolution lease id条件续租并创建 claim；任一条件失败时 handler不得执行，已被接管的旧 owner必须停止

#### Scenario: Tool claim 未完成时进入人工复核
- **WHEN** private lease存在且 tool execution claim状态为 executing、没有 completed result ref
- **THEN** internal resolution state变为 `needs_review`，public approval保持 waiting，API返回稳定 `409 approval.execution_needs_review`且不自动重放 handler

#### Scenario: 真实结果后再公开 resolve
- **WHEN** approved tool execution持久化 completed result并使原 run成功完成
- **THEN** approval public status原子更新为 approved并封存 private lease/operation；重复 resolve返回现有 409语义且不重复执行

#### Scenario: 已批准动作确定性失败仍完成 approval resolution
- **WHEN** approve已取得 private lease，`ToolRegistry.call_approved`恰好调用 handler一次并持久化确定性 failed result，原 run进入唯一 failed terminal
- **THEN** public approval最终更新为 approved，private lease被封存，只发布一次有效 approved resolution event/audit，且不得进入仅用于结果不确定状态的 `needs_review`

#### Scenario: Evidence 基础设施失败由真实 resolve 重试补偿
- **WHEN** approve或 deny已持久化仲裁/确定性结果，但 `run.resumed`、terminal或 `approval.resolved` event sink写入失败或丢失确认
- **THEN** repository把尚未收口的 approve/deny标记为私有 pending state；下一次真实 APR-002调用在继续返回既有 409 public语义前，使用同一 lease、稳定 event id和既有 tool result补齐 evidence，handler不重放，audit和有效 terminal/resolution event均恰好一份

#### Scenario: Worker 启动恢复 pending enqueue
- **WHEN** service worker启动并查询到 active approve lease处于 `resolution_state=claimed`与 `enqueue_pending`、没有 tool claim且私有 reviewer/decision/规范化 request hash字段完整
- **THEN** recovery使用保存的 operation、完整 fingerprint与首次 request correlation幂等 enqueue并更新 queued/message ref；fingerprint不完整、其他 enqueue state、不同 tenant、deny、terminal、needs_review或过期已接管 lease不被补投
