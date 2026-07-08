## ADDED Requirements

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
- **THEN** API 按 tenant/identity 可见性返回 Phase 6 public descriptor DTO，未授权或无权限时返回 401/403 `ApiErrorEnvelope`，且不暴露本地绝对路径、secret、callable 或 provider client

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
service-app SHALL 提供 approval HTTP routes 和 CLI，支持按 run list/read approval、approve、deny，并通过统一 `ApiErrorEnvelope` 表达认证错误、权限错误、资源不存在和状态冲突。approval 状态机 MUST 防止重复 approve/deny、跨 run resolve、错误 resume token 推进其他 run。

#### Scenario: 列出等待审批项
- **WHEN** run 因危险动作进入等待审批状态
- **THEN** `GET /api/v1/runs/{run_id}/approvals` 和 `agent-harness approvals list` 返回 approval id、status、action、resource、reason、tenant/agent/run/trace 摘要和 `request_id`

#### Scenario: approve 后恢复 run
- **WHEN** 已认证审批人对 waiting approval 调用 approve
- **THEN** approval 状态变为 approved，audit log 记录审批人、动作、结果和 trace，runtime 使用关联 checkpoint/resume seam 推进原 run

#### Scenario: deny 后按策略失败或 fallback
- **WHEN** 已认证审批人对 waiting approval 调用 deny
- **THEN** approval 状态变为 denied，目标危险动作不执行，run 按 policy 配置进入 failed 或 fallback，并写入 audit evidence

#### Scenario: 重复 resolve 返回状态冲突
- **WHEN** 调用方对已 approved 或 denied 的 approval 再次 approve/deny
- **THEN** API 返回 409 `ApiErrorEnvelope`，CLI 非零退出，且 run 状态和 audit log 不被重复改写

### Requirement: Audit log 记录 policy 与 approval 证据
系统 SHALL 提供 audit service，用于记录认证、policy decision、approval required/resolved、危险动作 outcome 和 resume 关联证据。audit payload MUST 包含 tenant、user、session、agent、run、trace、request、action、resource、decision/result 和 timestamp；MUST NOT 包含 secret、token、cookie、provider 原始响应或完整大 payload。

#### Scenario: policy decision 写 audit
- **WHEN** `PolicyEngine` 返回 allow、deny 或 require_approval
- **THEN** audit log 可按 tenant/run/trace/action 读取到 decision、reason、matched rule summary 和 request_id

#### Scenario: secret 不进入 audit payload
- **WHEN** policy context、approval reason 或 API header 中包含 token、cookie、password 或 secret-like 字段
- **THEN** audit payload 和 error envelope 只保留脱敏摘要，不写入原始 secret

### Requirement: Auth、policy 和 approval endpoint 契约先于实现扩展
`API-Contract.md` SHALL 在 route 实现前把 `APR-001`、`APR-002` 和 `POL-001` 扩展为完整 endpoint 条目，并更新 `AGT-001` 的 Phase 7 认证/可见性过滤要求；文档 MUST 明确认证、请求/响应 schema、幂等性、副作用、错误码、安全规则和验证要求。局部 OpenAPI drift tests MUST 覆盖 agents list、approval/policy 新增 paths、401/403、`ApiErrorEnvelope`、approval 状态冲突和 `request_id`。

#### Scenario: OpenAPI 包含 Phase 7 endpoint
- **WHEN** 生成 service-app OpenAPI schema
- **THEN** `/api/v1/runs/{run_id}/approvals`、`/api/v1/runs/{run_id}/approvals/{approval_id}` 和 `/api/v1/policies/check` 存在预期 method、request/response schema 和错误 envelope

#### Scenario: Contract tests 防止契约漂移
- **WHEN** Phase 7 contract tests 运行
- **THEN** tests 同时检查 `API-Contract.md` 条目、FastAPI route/OpenAPI schema、runtime behavior、agent list 可见性和 error envelope，不允许文档与运行时只改一边

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
