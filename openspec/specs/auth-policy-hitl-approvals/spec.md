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
