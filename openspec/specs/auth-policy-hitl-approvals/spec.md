# auth-policy-hitl-approvals Specification

## Purpose
定义 API 认证、PolicyEngine、InputGuardrail、HITL approval 和 audit evidence 的长期契约，使 run 创建、agent list、policy check、approval 生命周期和默认危险动作策略在 API、CLI 与 runtime seam 中保持一致。
## Requirements
### Requirement: API 认证注入稳定身份上下文
service-app SHALL 支持 API Key / Bearer Token 认证，并在受保护 P0 API 进入 runtime、registry、policy 或 approval seam 前注入 `IdentityContext`。未启用多租户认证时，local/dev 调用 SHALL 使用默认身份 `tenant_id="default"`、`user_id="local-user"`、显式 `session_id`、roles、permissions 和 `auth_method`；无效 token MUST 返回 `ApiErrorEnvelope`，且不得创建 run、approval、eval case、audit side effect 或 provider call。

#### Scenario: 无效 Bearer Token 不创建 run
- **WHEN** 调用方使用无效 `Authorization: Bearer <token>` 请求 `POST /api/v1/agents/{agent_id}/runs`
- **THEN** API 返回 401 `ApiErrorEnvelope`，响应包含 `request_id`，且同一 storage/event seam 中没有新增 run、checkpoint、event、approval 或 audit side effect

#### Scenario: 默认 local identity 被注入 run
- **WHEN** local/dev 配置未启用多租户认证并创建 run
- **THEN** run、session、trace/audit/event payload 至少携带 `tenant_id="default"`、`user_id="local-user"` 和可追踪 `session_id`

#### Scenario: 受保护读取按身份检查可见性
- **WHEN** 已认证调用方读取 run detail、events、approval 或 policy check 结果
- **THEN** API 根据 `IdentityContext` / `PermissionContext` 判断可见性，不暴露其他 tenant 的资源或内部 handle

#### Scenario: Agent list 按身份过滤
- **WHEN** 已认证调用方请求 `GET /api/v1/agents`
- **THEN** API 按 tenant/identity 可见性返回 registry public descriptor DTO，未授权或无权限时返回 401/403 `ApiErrorEnvelope`，且不暴露本地绝对路径、secret、callable 或 provider client

### Requirement: PolicyEngine 输出 allow、deny 或 require_approval
package SHALL 暴露 `PolicyEngine`，输入 actor、resource、action 和 context，输出稳定 `PolicyDecision`，其 decision MUST 为 `allow`、`deny` 或 `require_approval`。Policy provider SHALL 至少包含 YAML provider 和 DB provider interface；默认开发策略 SHALL 允许 default tenant 的常规操作，但危险动作默认可配置为 `require_approval` 或 `deny`。

#### Scenario: 常规动作允许
- **WHEN** default tenant 的 authenticated actor 对普通 run 读取动作执行 policy check
- **THEN** `PolicyEngine` 返回 `allow`，并给出 reason、matched rule summary 和 audit metadata

#### Scenario: shell 动作默认要求审批
- **WHEN** actor 请求执行 `shell.execute` 或等价危险动作
- **THEN** 默认 policy 返回 `require_approval`，并包含 approval action、resource、reason、tenant/user/session/agent/run/trace 关联字段

#### Scenario: deny 决策阻止动作执行
- **WHEN** policy provider 返回 `deny`
- **THEN** 调用 seam 不执行目标动作，返回 403 或业务 denial result，并写入 policy/audit 证据

#### Scenario: policy check API 可验证三态决策
- **WHEN** 调用 `POST /api/v1/policies/check`
- **THEN** 响应使用稳定 DTO 表示 actor/resource/action/context、decision、reason、matched rules、request_id 和 audit ref，OpenAPI schema 覆盖 200、401、403 和 `ApiErrorEnvelope`

### Requirement: InputGuardrail 在 run 创建前记录注入风险
系统 SHALL 在用户/API/CLI 输入进入 `RunOrchestrator` 前执行轻量 `InputGuardrail`，检测明显 prompt injection、越权指令和跨信任边界输入，并输出可序列化 guardrail/policy decision payload。guardrail 结果 MUST 写入 trace/audit evidence；阻断时不得创建不可恢复的半截 run。

#### Scenario: 明显 prompt injection 被记录并走策略
- **WHEN** run create 输入包含明显 prompt injection 或越权指令
- **THEN** `InputGuardrail` 写入检查结果、trust marker 和 audit metadata，并按 policy 返回 allow、deny 或 require_approval

#### Scenario: guardrail deny 不创建半截 run
- **WHEN** `InputGuardrail` / policy 对 run create 输入返回 deny
- **THEN** API 返回 403 `ApiErrorEnvelope` 或稳定 denial response，且不创建 run、checkpoint、approval 或不可恢复 side effect

#### Scenario: guardrail require_approval 进入审批等待
- **WHEN** `InputGuardrail` / policy 对 run create 输入返回 require_approval
- **THEN** 系统创建 approval、发布 `approval.required` evidence，并让 run 进入等待 checkpoint/resume 状态

### Requirement: 默认危险动作进入 HITL approval
系统 SHALL 提供默认 require_approval 清单，覆盖删除文件或批量改文件、执行 shell 命令、访问非工作区路径、外部网络或 MCP 连接、对外发送消息/工单/邮件、单次模型调用预计超过预算阈值、写入 approved eval dataset 和修改 policy。调用方 SHALL 能通过 YAML provider 或 DB provider interface 覆盖默认策略。

#### Scenario: 默认清单产生 approval.required
- **WHEN** runtime、tool 替身、model budget 或 policy check seam 遇到默认危险动作
- **THEN** 系统创建 approval 记录，发布 `approval.required` 或等价 CanonicalEvent，并让 run 进入等待 checkpoint/resume 状态

#### Scenario: policy 修改本身需要审批
- **WHEN** actor 请求修改 policy rule 或默认策略配置
- **THEN** 默认 policy 返回 `require_approval`，且 approval/audit 记录显示目标资源为 policy

### Requirement: Approval API 和 CLI 管理审批生命周期
service-app SHALL提供 approval HTTP routes和 CLI，支持按 run list/read approval、approve、deny，并通过统一 `ApiErrorEnvelope`表达认证错误、权限错误、资源不存在和状态冲突。approve与 deny MUST通过同一 repository条件更新做原子 resolution仲裁：deny先赢时目标动作不执行，但 public status在唯一 `approval.resolved` 与对应 failed/fallback terminal按 ordered outbox durable完成前保持 `waiting`，完成后才变为 `denied`；approve lease先赢时 public status在动作得到持久化确定性结果、唯一 approved resolution与对应 terminal按同一顺序 durable完成前保持 `waiting`，并发 deny返回 `409 approval.resolution_in_progress`。local/raw `claimed` lease只有在 owner timeout到期且不存在 tool execution claim时才能由后续真实 API/CLI resolve重试原子接管并换发 resolution lease id；活跃 lease和已有 claim不得被抢占。service profile的 approve enqueue是 active-lease 409规则的窄例外：首次 approve在同一事务写 resolution lease id、稳定 operation/首次 request correlation、私有 reviewer/decision/规范化 request hash、`resolution_state=claimed`与 `enqueue_pending`；enqueue失败返回 503 `approval.enqueue_unavailable` `ApiErrorEnvelope`。仅当私有 resolution state仍为 pre-execution `claimed`、enqueue state为 `enqueue_pending|queued`、尚无 tool claim，且本次 reviewer、decision、规范化 request hash与私有 fingerprint全部相同时，APR-002重试 MUST复用原 lease/operation并可返回 202 waiting/queued摘要，不能换发 lease或暴露私有 fingerprint/enqueue state；API retry只补投 `enqueue_pending`，`queued`复用既有 message/ref且不得创建第二条 message。worker recovery也只补投 `resolution_state=claimed`、active `enqueue_pending`、无 tool claim且持久 fingerprint完整的 lease。worker在启动 DBOS workflow前 MUST以 CAS把 `resolution_state`从 `claimed`迁移为 `execution_owned`并持久化 workflow owner/ref；此后 API retry不得再走 enqueue窄例外。`execution_owned` lease过期且仍无 tool claim时，只有 fingerprint匹配的真实 APR-002重试可以 takeover：同一事务先把旧 operation/message/workflow refs写入审计 evidence，再换发新的 resolution lease id、按新 lease id派生新 operation、把本次真实 request id固化为该新 operation的首次 `resolution_request_id`、把已验证 reviewer/decision/规范化 request hash重新绑定新 lease、清空当前 active message/workflow refs，并回到 `claimed+enqueue_pending`；后续 retry不得覆盖新 operation的首次 request correlation，旧 operation/DBOS owner因 lease id不匹配 fail closed。不同 fingerprint、已有 tool claim、其他私有 state或并发 deny仍返回既有409；活跃 tool claim返回 `approval.resolution_in_progress`，执行结果不确定并已进入私有 `needs_review`的 claim返回 `approval.execution_needs_review`。动作无论 completed或确定性 failed，人工批准决定只有在 ordered evidence 完成后 public status才变为 `approved`；只有执行结果不确定时才继续 `waiting`并进入不公开的私有 `needs_review`。基础设施 evidence失败 MUST保留私有 pending/outbox state；startup/runtime recovery与后续真实 API/CLI resolve重试 MUST只补投稳定 event id，不重放 tool/provider。状态机 MUST防止重复 approve/deny、跨 run resolve、错误 resume token推进其他 run、旧 owner越过 fencing、重复工具副作用和第二个有效 resolution event/audit/terminal。

#### Scenario: 列出等待审批项
- **WHEN** run因危险动作进入等待审批状态，或仲裁已完成但 ordered evidence尚未全部 durable
- **THEN** `GET /api/v1/runs/{run_id}/approvals`和 `agent-harness approvals list`返回 approval id、public `waiting` status、action、resource、reason、tenant/agent/run/trace摘要和 `request_id`，不公开 private lease、enqueue、outbox或 internal state

#### Scenario: Approve 赢得仲裁后恢复原 run
- **WHEN** 已认证审批人对 waiting approval调用 approve且 repository条件更新先取得 private resolution lease
- **THEN** public approval继续保持 waiting；local模式直接通过 runtime resume，service模式返回 202 waiting/queued并由 worker恢复；并发 deny返回 `409 approval.resolution_in_progress`且不得改写 public status或发布第二个 resolution event

#### Scenario: 已批准动作以确定性结果完成 resolution
- **WHEN** approved continuation恰好执行一次，并持久化 completed或确定性 failed result
- **THEN** ordered outbox先持久化唯一 approved resolution event/audit、再持久化对应唯一 run terminal；二者完成后 approval public status才变为 approved并封存 private lease，确定性 failed不进入 needs_review

#### Scenario: 不确定执行结果保持等待人工复核
- **WHEN** tool execution claim已进入 executing，但进程在持久化 completed/failed result前中断
- **THEN** private resolution state变为 needs_review，public approval保持 waiting，不自动重放 handler，也不伪造 approved resolution或terminal

#### Scenario: Deny 赢得仲裁后有序失败或 fallback
- **WHEN** 已认证审批人先对 waiting approval调用 deny
- **THEN** repository原子保存 deny仲裁与 ordered outbox，目标危险动作不执行且 service queue/DBOS operation为零；public status先保持 waiting，唯一 denied resolution/audit与 failed/fallback terminal按稳定 event id依次 durable后才变为 denied，后续 approve返回 `409 approval.invalid_transition`

#### Scenario: 重复 public resolve 返回状态冲突
- **WHEN** 调用方对已 approved或 denied的 approval再次 approve/deny
- **THEN** API返回 409 `ApiErrorEnvelope`，CLI非零退出，且 run、tool handler、有效 resolution event、terminal和 audit log不被重复改写；若私有 state标记为 evidence pending，本次真实调用先使用既有 result与稳定 event id触发内部幂等补投再返回原 409，但不得重放 handler或变成 public幂等成功

#### Scenario: Service approve enqueue 失败可重试
- **WHEN** service approve lease与 enqueue_pending已提交但 Redis不可用
- **THEN** 首次调用返回 503 `approval.enqueue_unavailable`；相同 reviewer/decision/request hash重试复用原 lease/operation/首次 request id并在成功时返回 202 waiting/queued，public `ApprovalRecord.status`仍为 waiting且不包含私有 enqueue字段

### Requirement: Approval resume 使用私有 resolution lease 协调执行
approval repository SHALL为 waiting approval提供不进入 public `ApprovalRecord`/OpenAPI的私有 resolution lease。lease id本身是 fencing identity，MUST具有可配置 owner timeout且每次合法接管都换发新的 resolution lease id。service approve SHALL在 lease事务内额外保存稳定 operation id、首次 request id、reviewer id、decision、规范化 request hash、`enqueue_pending|queued`、`resolution_state=claimed|execution_owned`与 message/workflow refs；这些字段不得进入 public status/DTO或普通 metadata。pre-execution `claimed`与 `execution_owned` MUST通过 repository CAS互斥：前者才允许 enqueue retry/recovery，后者表示 DBOS owner/ref已耐久持久化且只能由同 owner恢复或在超时无 tool claim时换发新 lease/operation接管。未过期 lease和已有 claim不得被抢占。public status MUST在已批准动作得到持久化确定性结果、`approval.resolved` 与对应 run terminal 按 durable ordered outbox 完成前保持 `waiting`；结果无论 completed或确定性 failed，人工批准决定完成后均变为 `approved`，deny后变为 `denied`。只有执行结果不确定时 public status才继续 `waiting`并进入私有 `needs_review`；terminal/resolution evidence基础设施失败只能进入可补偿私有 pending state。不得把内部 `claimed`、`execution_owned`、`enqueue_pending`、`queued`、`recovery_pending`、`denied_pending`、`executing`或 `needs_review`暴露为新的 public status。

#### Scenario: Approve 原子取得私有 lease
- **WHEN** 第一个 approve请求处理 public status为 waiting且没有有效 resolution lease的 approval
- **THEN** repository以条件更新写 private lease id、claimed timestamp和 internal state；service模式同事务写 operation/首次 request/reviewer/decision/request hash/`enqueue_pending`，public status仍为 waiting，并发请求不能取得第二 lease

#### Scenario: Deny 先赢得 resolution 仲裁
- **WHEN** deny先以 repository条件更新取得唯一 resolution 仲裁，随后 approve请求到达
- **THEN** approve不能取得 private lease，返回稳定 `409 approval.invalid_transition`，tool handler执行计数保持零；deny把稳定 `approval.resolved` event id与所需 terminal 写入 ordered outbox，resolution 先于 terminal，二者 durable 后 public status才变为 denied

#### Scenario: Approve lease 先赢得 resolution 仲裁
- **WHEN** approve先以 repository条件更新取得 private lease，随后并发 deny请求到达
- **THEN** deny返回稳定 `409 approval.resolution_in_progress`，public status在 approved tool execution与 ordered evidence完成前仍为 waiting，deny不修改 public status、不创建 tool claim，且同一 approval最终最多产生一次有效 resolution event/audit

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

#### Scenario: 真实结果后按顺序公开 resolve
- **WHEN** approved tool execution持久化 completed result并使原 run可确定成功完成
- **THEN** repository以稳定 event id把唯一 `approval.resolved` 与唯一 `run.completed` terminal写入同一 ordered outbox group，resolution 排在 terminal 前；二者 durable 后 public status才更新为 approved并封存 private lease/operation，重复 resolve返回现有 409语义且不重复执行

#### Scenario: 已批准动作确定性失败仍完成 approval resolution
- **WHEN** approve已取得 private lease，`ToolRegistry.call_approved`恰好调用 handler一次并持久化确定性 failed result
- **THEN** repository先补投唯一 approved resolution event/audit，再补投唯一 `run.failed` terminal；二者 durable 后 public approval最终更新为 approved并封存 private lease，且不得进入仅用于结果不确定状态的 `needs_review`

#### Scenario: Evidence 基础设施失败由 outbox 恢复
- **WHEN** approve或 deny已持久化仲裁/确定性结果，但 `run.resumed`、`approval.resolved` 或 terminal event sink写入失败、确认丢失或进程退出
- **THEN** repository保留 ordered outbox pending state；startup/runtime recovery与下一次真实 APR-002调用都使用同一 lease、稳定 event id和既有 tool result幂等补投，resolution 必须先于 terminal，全部 prerequisite evidence完成后才公开 resolution；handler不重放，audit、有效 resolution与terminal均恰好一份

#### Scenario: Worker 启动恢复 pending enqueue
- **WHEN** service worker启动并查询到 active approve lease处于 `resolution_state=claimed`与 `enqueue_pending`、没有 tool claim且私有 reviewer/decision/规范化 request hash字段完整
- **THEN** recovery使用保存的 operation、完整 fingerprint与首次 request correlation幂等 enqueue并更新 queued/message ref；fingerprint不完整、其他 enqueue state、不同 tenant、deny、terminal、needs_review或过期已接管 lease不被补投

### Requirement: Audit log 记录 policy 与 approval 证据
系统 SHALL 提供 audit service，用于记录认证、policy decision、approval required/resolved、危险动作 outcome 和 resume 关联证据。audit payload MUST 包含 tenant、user、session、agent、run、trace、request、action、resource、decision/result 和 timestamp；MUST NOT 包含 secret、token、cookie、provider 原始响应或完整大 payload。

#### Scenario: policy decision 写 audit
- **WHEN** `PolicyEngine` 返回 allow、deny 或 require_approval
- **THEN** audit log 可按 tenant/run/trace/action 读取到 decision、reason、matched rule summary 和 request_id

#### Scenario: secret 不进入 audit payload
- **WHEN** policy context、approval reason 或 API header 中包含 token、cookie、password 或 secret-like 字段
- **THEN** audit payload 和 error envelope 只保留脱敏摘要，不写入原始 secret

### Requirement: Auth、policy 和 approval endpoint 契约先于实现扩展
`API-Contract.md` SHALL 在 route 实现前把 `APR-001`、`APR-002` 和 `POL-001` 扩展为完整 endpoint 条目，并更新 `AGT-001` 的认证/可见性过滤要求；文档 MUST 明确认证、请求/响应 schema、幂等性、副作用、错误码、安全规则和验证要求。局部 OpenAPI drift tests MUST 覆盖 agents list、approval/policy paths、401/403、`ApiErrorEnvelope`、approval 状态冲突和 `request_id`。

#### Scenario: OpenAPI 包含认证、policy 与 approval endpoint
- **WHEN** 生成 service-app OpenAPI schema
- **THEN** `/api/v1/runs/{run_id}/approvals`、`/api/v1/runs/{run_id}/approvals/{approval_id}` 和 `/api/v1/policies/check` 存在预期 method、request/response schema 和错误 envelope

#### Scenario: Contract tests 防止契约漂移
- **WHEN** auth/policy/approval contract tests 运行
- **THEN** tests 同时检查 `API-Contract.md` 条目、FastAPI route/OpenAPI schema、runtime behavior、agent list 可见性和 error envelope，不允许文档与运行时只改一边

### Requirement: Approval 与 audit 继承 run canonical trace
Approval service SHALL 从已持久化 run execution context 取得非空 `trace_id`，并写入 ApprovalRecord、approval required/resolved event 和 audit evidence。HTTP/CLI body、policy result 或 tool metadata MUST NOT 覆盖该值；缺失 canonical trace MUST 在创建 approval 前 fail closed。

#### Scenario: 创建 approval 继承 run trace
- **WHEN** policy 为已绑定 canonical trace 的 run 返回 `require_approval`
- **THEN** waiting ApprovalRecord、checkpoint、required event 与 audit evidence 的 trace 逐值一致且不可为空

#### Scenario: Caller 不能覆盖 approval trace
- **WHEN** approve/deny 请求携带不同 trace 或相关 metadata 试图覆盖关联
- **THEN** service 忽略不受信覆盖并使用 persisted run trace，或在协议不允许该字段时返回 validation error，且不改变 approval 仲裁语义

#### Scenario: 缺失 run trace 时 fail closed
- **WHEN** legacy/损坏 run 在 backfill 完成后仍缺少 canonical trace并尝试创建 approval
- **THEN** service 在 checkpoint、approval、audit 和危险动作副作用前返回稳定错误

### Requirement: Policy review threshold 不得绕过 shared hard limit
单次model/embedding调用的`policy review threshold` SHALL是独立软阈值，只能产生可追踪allow、有限fallback、deny或`require_approval`；`max_tokens_per_run`与`max_cost_usd_per_run` MUST是不可由审批提高、重置或覆盖的parent execution tree共享硬上限。系统 MUST先在frozen config内完成context/route降级并取得trusted finite intent；无有限上界或intent静态不可能满足hard limit时直接hard reject。Hard-eligible intent才可进入软阈值策略；fallback MUST回到route/trusted-intent步骤并有限终止。Allow或approve后 MUST在任何外部副作用前以当前余额执行shared-ledger原子reservation；approval不预约或持有额度，等待期间余额变化必须在resume时重检。

#### Scenario: Approval 不能覆盖 hard limit
- **WHEN** operation超过软review threshold并获得approve，但等待期间其他direct/delegation claim使当前parent余额不足
- **THEN** continuation在provider/child/queue副作用前的原子reservation处hard reject，approval不提高或重置hard limit，外部副作用计数为零

#### Scenario: 无可信上界不进入审批
- **WHEN** 实际route无法为启用的token或cost hard limit提供trusted finite worst-case bound
- **THEN** 系统直接hard reject，不创建用于绕过该失败的approval，也不调用provider、创建child或投递queue；只允许封闭脱敏内部rejection evidence

#### Scenario: Soft fallback 重新进入有界顺序
- **WHEN** soft threshold策略选择fallback route
- **THEN** 系统在frozen route/price snapshot内重新计算actual route trusted intent并再次评估soft policy，循环必须由封闭fallback列表有限终止，最终仍须通过shared-ledger原子reservation

### Requirement: 模型工具审批绑定完整循环身份且不能扩权
模型工具 loop 的 approval SHALL 使用版本化 exact arguments/continuation，绑定 loop id、turn ordinal、tool call id、tool name、arguments/schema/catalog digests、action/resource、tenant/user/session/agent/run/request/trace、冻结 hard bounds 和原 policy decision。Approval record、checkpoint、grant hash、active resolution lease 与当前 bound context MUST 逐值及逐 digest 一致；grant SHALL 只能批准原 intent，不能增加工具、arguments、schema、预算或 deadline。

#### Scenario: Matching approval 恢复原 loop
- **WHEN** reviewer批准waiting tool intent且grant/lease/checkpoint/current bound context完全匹配
- **THEN** runtime恢复同一loop/turn/tool call并重新执行current hard bounds
- **AND** grant只跳过一次原soft policy gate

#### Scenario: 过期或扩权grant零工具副作用
- **WHEN** grant过期、已消费、跨tenant/run、替换arguments/schema/action/resource或提高hard bounds
- **THEN** resume在claim/handler/next model turn前关闭失败
- **AND** approval public状态和既有evidence不被伪造改写

### Requirement: Approval waiting 阻止模型循环和 run terminal
当模型工具 policy 返回 `require_approval` 时，系统 SHALL 持久化 approval、checkpoint 和 stable `approval.required` evidence，并使 loop/run 保持 waiting。Waiting 期间 MUST NOT 建立 tool execution claim、调用 handler、组装工具结果、进入下一 model turn 或发布 run terminal。Deny/resolution evidence SHALL 继续遵守既有 ordered outbox 与唯一 public status 规则。

#### Scenario: Waiting 期间零后续副作用
- **WHEN** approval 尚未被唯一仲裁并完成必要 evidence
- **THEN** model/tool/context调用计数保持在waiting前值
- **AND** run只暴露既有waiting approval摘要

#### Scenario: Deny 后不恢复循环
- **WHEN** deny赢得原子仲裁并完成ordered evidence
- **THEN** loop以确定失败收口且不调用handler或下一model turn

### Requirement: 工具循环approval恢复与durable loop/claim共同围栏
Approval resume SHALL在任何`run.resumed` event、tool claim takeover、handler或model调用前，交叉验证active resolution lease、ApprovalGrant、checkpoint、model_tool_loops row和nullable existing tool invocation claim。Loop/turn/tool-call/catalog/arguments/schema/action/resource、tenant/identity/run/request/trace、frozen bounds和approval lease/fingerprint MUST逐值一致。Existing completed/failed claim SHALL只返回exact结果；executing/needs-review claim SHALL拒绝takeover和handler重放。Existing claimed claim只有在tool execution lease已过期、binding一致，且owner UoW按`tool-handler-not-started-v1`原子换租并递增fence后才能继续；approval lease不能替代tool execution lease/fence。

#### Scenario: Matching grant与空claim恢复一次
- **WHEN**waiting loop、checkpoint、grant/lease匹配且不存在tool claim
- **THEN**runtime以原tool_call_id创建唯一claim并执行一次

#### Scenario: Existing completed claim只补approval evidence
- **WHEN**tool result已completed但approval resolved/terminal evidence尚未published
- **THEN**recovery返回既有result并只补ordered evidence
- **AND**handler与model调用计数不增加

#### Scenario: Existing executing claim阻止takeover
- **WHEN**approval lease过期但同tool_call_id claim仍executing或needs-review
- **THEN**API/worker返回resolution in progress或execution needs review
- **AND**不得换发能执行第二次handler的新lease/tool call identity

#### Scenario: Approved claimed claim按工具栅栏安全接管
- **WHEN**approval grant仍匹配且existing tool claim为`claimed`、tool execution lease已过期
- **THEN**runtime以同一tool_call_id原子写入可信未开始proof并轮换tool lease/fence
- **AND**旧owner和approval lease本身都不能绕过新的tool fence调用handler

### Requirement: Approval等待与恢复不重置loop边界
Approval record/checkpoint SHALL保存原loop frozen bounds和next ordinal identity。Resume时runtime SHALL复用原deadline、turn/count、catalog和累计usage，并重新检查current hard policy/owner balance；current config或reviewer decision MUST只能进一步拒绝，不能提高上限或改写下一step。

#### Scenario: 等待跨过deadline后拒绝
- **WHEN**approval通过时原loop absolute deadline已到达
- **THEN**resume在tool claim/handler前以limit/cancelled稳定终态关闭
- **AND**不创建新loop或延长deadline
