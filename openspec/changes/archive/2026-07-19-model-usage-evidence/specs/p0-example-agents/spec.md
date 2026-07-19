## MODIFIED Requirements

### Requirement: Dev assistant 的危险工具调用受 Policy 与 HITL 控制
dev assistant SHALL 通过 `ToolRegistry` 调用 allowlisted file/shell tool；危险动作 MUST 经过 `PolicyEngine`。决策为 `require_approval` 时，公共 run 链 MUST 创建 waiting checkpoint 和真实 approval record，checkpoint 只保存脱敏 continuation、pending tool/action/resource、arguments artifact ref 与 hash、executor ref 和 tenant/identity/run/trace 绑定。approve MUST 通过 `ApprovalService` 生成匹配该 continuation 的 `ApprovalGrant`，重新进入同一 `AgentExecutor`/`ToolRegistry` 执行待批动作，并通过持久化 `approval_id` execution claim 保证正常审批链只执行一次；deny MUST 让原 run 失败且目标动作始终不执行。approve/deny 都 MUST 在公开状态仍为 waiting 时先持久化稳定 ID 的唯一 `approval.resolved`，再持久化对应 completed/failed/fallback terminal，只有两者均已确认后才公开 approval/run 终态；恢复只补投同一 outbox evidence，不得重放 provider、tool handler 或 continuation。policy response 中的摘要不得替代 approval record。

#### Scenario: 安全只读命令允许执行
- **WHEN** allowlist、workspace 和 policy 均允许只读命令
- **THEN** dev assistant 返回 tool result、policy decision 和 trace/audit refs

#### Scenario: 危险命令等待审批
- **WHEN** `shell.execute` 或写操作的 policy decision 为 `require_approval`
- **THEN** tool 不执行，run 进入 waiting 并写 checkpoint，系统创建可由 approvals CLI/API 读取的 approval record，记录关联的 run/resume token 摘要、identity、trace 和脱敏 audit evidence

#### Scenario: 审批后恢复原 run
- **WHEN** 人工通过 approvals CLI/API approve waiting action
- **THEN** `ApprovalService` 在公开状态仍为 waiting 时原子取得私有 resolution lease，校验 token、action、resource、arguments hash、tenant、identity、agent 和 run 后生成 `ApprovalGrant`；`RunOrchestrator` 用 checkpoint continuation 重新调用原 executor，`ToolRegistry` 执行待批动作恰好一次并持久化真实 result ref；系统随后先确认唯一 approved resolution，再确认唯一 completed/failed terminal，最后公开 approved 与 run 终态

#### Scenario: 已批准动作返回确定性失败
- **WHEN** approved continuation 的 tool handler 执行一次并返回已持久化的确定性 failed result
- **THEN** 系统先持久化唯一 approved resolution event/audit，再发布唯一 failed terminal，二者确认后公开 approval 更新为 approved 以表达“已允许执行”；私有 lease 被封存，不把已知失败误标为 `needs_review`，也不重放 handler

#### Scenario: 重复 resolve 不重复执行
- **WHEN** 同一 approval 被并发或重复 approve，或同一 resume token 被再次提交
- **THEN** 唯一 `approval_id` execution claim 只允许一个调用进入 tool handler；后续调用返回已完成结果或 `approval.invalid_transition`，handler 执行计数保持一，稳定 outbox 只产生一组 resolution/terminal，audit/trace 不伪造第二次执行

#### Scenario: 原始 resume token 不能绕过 ApprovalService
- **WHEN** 调用方把 dev assistant approval checkpoint 的原始 resume token 直接提交到公开 `RUN-005`
- **THEN** API 返回 `409 run.invalid_transition`，handler 执行计数为零，run/approval 状态不变；只有 `APR-002` 取得私有 lease、生成匹配 `ApprovalGrant` 后，内部 resume seam 才能执行待批动作

#### Scenario: 执行中断状态不自动重放
- **WHEN** 进程在 execution claim 已持久化但 tool result 尚未持久化时中断
- **THEN** 恢复路径把该 action 标记为 `needs_review` 并保留 claim/trace，不自动再次执行具有外部副作用的动作

#### Scenario: Approval lease 与 tool claim 之间中断可恢复
- **WHEN** 进程在私有 resolution lease 已写入但唯一 tool execution claim 尚未创建时中断
- **THEN** 公开 approval 仍为 waiting，恢复路径复用同一 lease 和 checkpoint 创建唯一 tool claim；因为 handler 尚未进入，该恢复不会丢失动作或造成重复执行

#### Scenario: 拒绝动作不执行
- **WHEN** policy deny、tool/agent allowlist 拒绝请求，或 reviewer deny waiting action
- **THEN** 目标命令或文件变更没有发生；policy/allowlist deny 返回稳定 error code，reviewer deny 则只持久化 denied resolution 与 failed/fallback terminal，二者确认前公开状态保持 waiting
