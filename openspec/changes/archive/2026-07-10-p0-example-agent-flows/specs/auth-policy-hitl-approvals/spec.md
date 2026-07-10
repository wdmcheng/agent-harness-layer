## ADDED Requirements

### Requirement: Approval resume 使用私有 resolution lease 协调执行
approval repository SHALL 为 waiting approval 提供不进入 public `ApprovalRecord` / OpenAPI 的私有 resolution lease。lease MUST 具有可配置的 owner timeout 和每次接管都换发的 fencing id；只有已过期且尚无 tool execution claim 的 raw `claimed` lease 可以被后续真实 resolve 原子接管，未过期 lease 和已有 claim 的 lease 都不得被抢占。public status MUST 在已批准动作得到持久化的确定性结果并让 run 进入 terminal 前保持 `waiting`；结果无论 completed 或确定性 failed，人工批准决定完成后均变为 `approved`，deny 后变为 `denied`。只有执行结果不确定时 public status 才继续保持 `waiting` 并进入私有 `needs_review`；terminal/resolution evidence 基础设施失败只能进入可补偿的私有 pending state，不能伪装成执行结果不确定。不得把内部 `claimed`、`recovery_pending`、`denied_pending`、`executing` 或 `needs_review` 暴露为新的 public status。

#### Scenario: Approve 原子取得私有 lease
- **WHEN** 第一个 approve 请求处理 public status 为 waiting 且没有有效 resolution lease 的 approval
- **THEN** repository 以条件更新写入 private lease id、claimed timestamp 和 internal state，public status 仍为 waiting；并发请求不能取得第二个 lease

#### Scenario: Deny 先赢得 resolution 仲裁
- **WHEN** deny 先以 repository 条件更新把 waiting approval 解析为 denied，随后 approve 请求到达
- **THEN** approve 不能取得 private lease，返回稳定 `409 approval.invalid_transition`，tool handler 执行计数保持零，且只记录一次有效 denied resolution event/audit

#### Scenario: Approve lease 先赢得 resolution 仲裁
- **WHEN** approve 先以 repository 条件更新取得 private lease，随后并发 deny 请求到达
- **THEN** deny 返回稳定 `409 approval.resolution_in_progress`，public status 在 approved tool execution 完成前仍为 waiting，deny 不修改 public status、不创建 tool claim，且同一 approval 最终最多产生一次有效 resolution event/audit

#### Scenario: 进程硬退出后的过期 lease 可安全接管
- **WHEN** 进程在 private resolution lease 已提交、但对应 `tool_invocations.approval_id` claim 尚未创建时硬退出，owner timeout 随后到期且真实 `APR-002` API/CLI 重试到达
- **THEN** repository 原子确认 approval 仍为 waiting、private state 仍为 raw `claimed`、lease 已过期且不存在 tool claim，再换发新的 fencing id 并从原 approval/checkpoint 继续；旧 owner 的 fencing id 不得创建 tool claim，handler 最终至多执行一次

#### Scenario: 活跃 lease 不可被并发重试抢占
- **WHEN** private resolution lease 尚未超过 owner timeout，或同一 approval 已存在 tool execution claim，另一个真实 `APR-002` API/CLI 重试到达
- **THEN** repository 不得换发 lease；public 调用继续返回 `409 approval.resolution_in_progress` 或现有确定性恢复语义，活跃 owner 的执行路径和唯一 claim 不被第二个调用改写

#### Scenario: Lease fencing 与 tool claim 在同一事务生效
- **WHEN** owner 准备在 handler 前创建唯一 `tool_invocations.approval_id` claim
- **THEN** repository MUST 在同一 UoW 内按当前 fencing id 条件续租并创建 claim；任一条件失败时 handler 不得执行，已被接管的旧 owner 必须停止

#### Scenario: Tool claim 未完成时进入人工复核
- **WHEN** private lease 存在且 tool execution claim 状态为 executing、没有 completed result ref
- **THEN** internal resolution state 变为 `needs_review`，public approval 保持 waiting，API 返回稳定冲突/等待摘要且不自动重放 handler

#### Scenario: 真实结果后再公开 resolve
- **WHEN** approved tool execution 持久化 completed result 并使原 run 成功完成
- **THEN** approval public status 原子更新为 approved 并清理/封存 private lease；重复 resolve 返回现有 409 语义且不重复执行

#### Scenario: 已批准动作确定性失败仍完成 approval resolution
- **WHEN** approve 已取得 private lease，`ToolRegistry.call_approved` 恰好调用 handler 一次并持久化确定性的 failed result，原 run 因该结果进入唯一 failed terminal
- **THEN** public approval 最终更新为 approved，表示人工已允许该动作继续而不表示动作执行成功；private lease 被封存，只发布一次有效 approved resolution event/audit，且不得进入仅用于结果不确定状态的 `needs_review`

#### Scenario: Evidence 基础设施失败由真实 resolve 重试补偿
- **WHEN** approve 或 deny 已持久化仲裁/确定性结果，但 `run.resumed`、terminal 或 `approval.resolved` event sink 在写入前失败或写入后丢失确认
- **THEN** repository 把尚未收口的 approve/deny 标记为私有 pending state；下一次真实 `APR-002` API/CLI 调用在继续返回既有 409 public 语义前，使用同一 lease、稳定 event id 和既有 tool result 补齐 terminal/resolution evidence，handler 不重放，approval audit 和有效 terminal/resolution event 均恰好一份

## MODIFIED Requirements

### Requirement: Approval API 和 CLI 管理审批生命周期
service-app SHALL 提供 approval HTTP routes 和 CLI，支持按 run list/read approval、approve、deny，并通过统一 `ApiErrorEnvelope` 表达认证错误、权限错误、资源不存在和状态冲突。approve 与 deny MUST 通过同一 repository 条件更新做原子 resolution 仲裁：deny 先赢时 public status 变为 `denied` 且目标动作不执行；approve lease 先赢时 public status 在动作得到持久化确定性结果并让 run 进入 terminal 前保持 `waiting`，并发 deny 返回 `409 approval.resolution_in_progress`。raw claimed lease 只有在 owner timeout 到期且不存在 tool execution claim 时才能由后续真实 API/CLI resolve 重试原子接管并换发 fencing id；活跃 lease 和已有 claim 不得被抢占。动作无论 completed 或确定性 failed，人工批准决定完成后 public status 均变为 `approved`；只有执行结果不确定时才继续 `waiting` 并进入不公开的私有 `needs_review`。基础设施 evidence 失败 MUST 保留私有 pending state，并由后续真实 API/CLI resolve 重试在返回既有 409 前触发内部补偿。状态机 MUST 防止重复 approve/deny、跨 run resolve、错误 resume token 推进其他 run、旧 owner 越过 fencing、重复工具副作用和第二个有效 resolution event/audit。

#### Scenario: 列出等待审批项
- **WHEN** run 因危险动作进入等待审批状态
- **THEN** `GET /api/v1/runs/{run_id}/approvals` 和 `agent-harness approvals list` 返回 approval id、status、action、resource、reason、tenant/agent/run/trace 摘要和 `request_id`，不公开 private lease/internal state

#### Scenario: Approve 赢得仲裁后恢复原 run
- **WHEN** 已认证审批人对 waiting approval 调用 approve 且 repository 条件更新先取得 private resolution lease
- **THEN** public approval 继续保持 waiting，runtime 使用关联 checkpoint/resume seam 执行原动作；并发 deny 返回 `409 approval.resolution_in_progress` 且不得改写 public status 或发布第二个 resolution event

#### Scenario: 已批准动作以确定性结果完成 resolution
- **WHEN** approved continuation 恰好执行一次，并持久化 completed 或确定性 failed result，使原 run 进入对应唯一 terminal
- **THEN** approval public status 变为 approved，private lease 被封存，audit log 记录审批人、动作、真实结果和 trace，且只产生一次有效 approved resolution event/audit；确定性 failed 不进入 needs_review

#### Scenario: 不确定执行结果保持等待人工复核
- **WHEN** tool execution claim 已进入 executing，但进程在持久化 completed/failed result 前中断
- **THEN** private resolution state 变为 needs_review，public approval 保持 waiting，不自动重放 handler，也不伪造 approved resolution

#### Scenario: Deny 赢得仲裁后失败或 fallback
- **WHEN** 已认证审批人先对 waiting approval 调用 deny
- **THEN** approval 状态变为 denied，后续 approve 无法取得 lease并返回 `409 approval.invalid_transition`，目标危险动作不执行，run 按 policy 配置进入 failed 或 fallback，并只写一次 denied resolution/audit evidence

#### Scenario: 重复 public resolve 返回状态冲突
- **WHEN** 调用方对已 approved 或 denied 的 approval 再次 approve/deny
- **THEN** API 返回 409 `ApiErrorEnvelope`，CLI 非零退出，且 run、tool handler、有效 resolution event 和 audit log 不被重复改写；若私有 state 标记为 evidence pending，本次真实调用先触发内部幂等补偿再返回原 409，但不得变成 public 幂等成功

## REMOVED Requirements

## RENAMED Requirements
