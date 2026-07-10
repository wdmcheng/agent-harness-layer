# p0-example-agents Specification

## Purpose
定义四个 P0 示例 Agent 从发现、离线执行、工具安全边界到真实 Eval 证据的长期契约，确保 RAG、工单分类、仓库分析与开发辅助流程通过公共 registry、runtime、model、tool、approval 和 eval seam 可重复验证。

## Requirements

### Requirement: 四个 P0 示例 Agent 可发现且离线运行
service-app SHALL 注册 RAG assistant、ticket triage、repo analyst 和 dev assistant 四个不同能力的示例 agent；每个示例 MUST 有完整 config、输入/输出 schema、工具策略、approved eval cases 和测试，并可在 fake model/local profile 下无真实 API key 运行。

#### Scenario: Registry 列出四个示例
- **WHEN** 开发者执行 `agent-harness agents list` 并指向模板 agent 目录
- **THEN** 输出包含 RAG assistant、ticket triage、repo analyst 和 dev assistant 四个稳定 `agent_id` 及 public descriptor

#### Scenario: Local run 产生 terminal trace
- **WHEN** 开发者用 local profile 执行 `agent-harness run <agent_id>` 调用任一 P0 示例
- **THEN** registry 解析该 agent 的受控 executor，`RunOrchestrator` 在 `RUN_STARTED` 后真实调用示例逻辑，并以该逻辑的 typed output 或 waiting/failed 结果推进 run，产生 `run_id`、可读取 trace evidence 和正确 terminal/waiting 状态，且不要求真实 provider key

### Requirement: RAG assistant 保留 citation 与不可信上下文边界
RAG assistant SHALL 通过公共 `RetrievalProvider` 查询 local BM25 或等价 fake，把结果转换为带 `source_ref`、citation 和 `trust_level=untrusted` 的 `ContextFragment`，并强制经过持久化 `ContextAssembler` 的预算裁剪与 assembly trace 后再调用 `ModelProvider`；回答 MUST 返回 citation，未命中时 MUST 明确说明没有来源。

#### Scenario: Local BM25 命中并返回引用
- **WHEN** local profile 在没有 PostgreSQL extension 的情况下查询已索引内容
- **THEN** RAG assistant 返回基于 BM25 结果的回答、至少一个 citation/source ref，以及 retrieval、context assembly、model trace evidence

#### Scenario: Prompt injection chunk 不覆盖指令
- **WHEN** retrieval result 内容包含要求覆盖 system、policy 或 developer 指令的文本
- **THEN** RAG assistant 通过 `ContextAssembler` 把该内容保留为 untrusted citation，不把它提升为控制指令，并在持久化 assembly trace 中保留 trust、截断和 input ref 标记

#### Scenario: 无检索结果时诚实降级
- **WHEN** retrieval provider 返回空结果
- **THEN** RAG assistant 返回 `no_source` 状态和无来源说明，不伪造 citation

### Requirement: Ticket triage 输出稳定结构化分类
ticket triage SHALL 把 ticket 文本转换为受 schema 校验的 category、priority、confidence 和 route/needs_review 结果，并通过 fake model evidence 证明离线执行。

#### Scenario: 已知 ticket 确定性分类
- **WHEN** 输入匹配已定义的 access、billing、bug 或 incident 分类信号
- **THEN** 输出通过 schema 校验并返回稳定 category、priority、route 和 model trace evidence

#### Scenario: 模糊 ticket 进入人工复核
- **WHEN** 输入缺少足够分类信号或 confidence 低于阈值
- **THEN** category 为 `unknown`、`needs_review=true`，不得伪造确定分类

### Requirement: Repo analyst 只通过 workspace file tool 分析仓库
repo analyst SHALL 只使用 allowlisted file read/search/list 公共 tool seam，MUST NOT 使用 shell；越界路径必须拒绝，长输出必须通过 `artifact_ref` 保留全量内容而不是截断后喂给 agent。

#### Scenario: Workspace 内分析成功
- **WHEN** 调用方要求搜索或读取 workspace 内文件
- **THEN** repo analyst 返回基于 untrusted tool result 的摘要、source refs 和 tool trace evidence

#### Scenario: Workspace 越界被拒绝
- **WHEN** 输入路径解析后越过 workspace root 或命中 ignore 规则
- **THEN** tool result 返回 `tool.workspace_denied`，repo analyst 不读取或回显目标内容

#### Scenario: 长结果外置为 artifact
- **WHEN** file result 超过 inline output 阈值
- **THEN** repo analyst 返回 `artifact_ref` 和截断摘要，同时完整结果保留在 artifact store

### Requirement: Dev assistant 的危险工具调用受 Policy 与 HITL 控制
dev assistant SHALL 通过 `ToolRegistry` 调用 allowlisted file/shell tool；危险动作 MUST 经过 `PolicyEngine`。决策为 `require_approval` 时，公共 run 链 MUST 创建 waiting checkpoint 和真实 approval record，checkpoint 只保存脱敏 continuation、pending tool/action/resource、arguments artifact ref 与 hash、executor ref 和 tenant/identity/run/trace 绑定。approve MUST 通过 `ApprovalService` 生成匹配该 continuation 的 `ApprovalGrant`，重新进入同一 `AgentExecutor`/`ToolRegistry` 执行待批动作，并通过持久化 `approval_id` execution claim 保证正常审批链只执行一次；deny MUST 让原 run 失败且目标动作始终不执行。policy response 中的摘要不得替代 approval record。

#### Scenario: 安全只读命令允许执行
- **WHEN** allowlist、workspace 和 policy 均允许只读命令
- **THEN** dev assistant 返回 tool result、policy decision 和 trace/audit refs

#### Scenario: 危险命令等待审批
- **WHEN** `shell.execute` 或写操作的 policy decision 为 `require_approval`
- **THEN** tool 不执行，run 进入 waiting 并写 checkpoint，系统创建可由 approvals CLI/API 读取的 approval record，记录关联的 run/resume token 摘要、identity、trace 和脱敏 audit evidence

#### Scenario: 审批后恢复原 run
- **WHEN** 人工通过 approvals CLI/API approve waiting action
- **THEN** `ApprovalService` 在 public status 仍为 waiting 时原子取得私有 resolution lease，校验 token、action、resource、arguments hash、tenant、identity、agent 和 run 后生成 `ApprovalGrant`；`RunOrchestrator` 用 checkpoint continuation 重新调用原 executor，`ToolRegistry` 执行待批动作恰好一次、持久化真实 result ref，并以该真实结果完成同一 run 后才把 approval 公开状态更新为 approved

#### Scenario: 已批准动作返回确定性失败
- **WHEN** approved continuation 的 tool handler 执行一次并返回已持久化的确定性 failed result
- **THEN** 原 run 发布唯一 failed terminal，public approval 更新为 approved 以表达“已允许执行”，private lease 被封存，只产生一次有效 approval resolution event/audit；系统不得把已知失败误标为 `needs_review` 或重放 handler

#### Scenario: 重复 resolve 不重复执行
- **WHEN** 同一 approval 被并发或重复 approve，或同一 resume token 被再次提交
- **THEN** 唯一 `approval_id` execution claim 只允许一个调用进入 tool handler；后续调用返回已完成结果或 `approval.invalid_transition`，handler 执行计数保持一，audit/trace 不伪造第二次执行

#### Scenario: 原始 resume token 不能绕过 ApprovalService
- **WHEN** 调用方把 dev assistant approval checkpoint 的原始 resume token 直接提交到公开 `RUN-005`
- **THEN** API 返回 `409 run.invalid_transition`，handler 执行计数为零，run/approval 状态不变；只有 `APR-002` 取得私有 lease、生成匹配 `ApprovalGrant` 后，内部 resume seam 才能执行待批动作

#### Scenario: 执行中断状态不自动重放
- **WHEN** 进程在 execution claim 已持久化但 tool result 尚未持久化时中断
- **THEN** 恢复路径把该 action 标记为 `needs_review` 并保留 claim/trace，不自动再次执行具有外部副作用的动作

#### Scenario: Approval lease 与 tool claim 之间中断可恢复
- **WHEN** 进程在私有 resolution lease 已写入但唯一 tool execution claim 尚未创建时中断
- **THEN** public approval 仍为 waiting，恢复路径复用同一 lease 和 checkpoint 创建唯一 tool claim；因为 handler 尚未进入，该恢复不会丢失动作或造成重复执行

#### Scenario: 拒绝动作不执行
- **WHEN** policy deny 或 tool/agent allowlist 拒绝请求
- **THEN** dev assistant 返回稳定 error code，目标命令或文件变更没有发生

### Requirement: 四个示例 Eval 真实执行并留下可复核证据
每个示例的 eval adapter SHALL 扩展现有 `EvalRunner` 的 approved case execution seam，使用 fake model 和受控 fake/local dependencies 执行公开 agent seam并比较 typed output；score MUST 先通过 `ScoreSink` 写 local evidence，再由 `TelemetryFacade` 执行可选 provider fan-out。所有 case MUST 确定性通过且产生 score 与 trace evidence，draft case MUST NOT 参与评分，provider failure MUST 只形成脱敏 degraded status。

#### Scenario: 四个 approved dataset 确定性通过
- **WHEN** 开发者运行四个示例的 fake-model eval
- **THEN** 每个 dataset 至少执行一个正常 case 和一个降级/安全 case，结果为 passed，输出 case 数、score summary 和 trace refs

#### Scenario: Draft case 不参与评分
- **WHEN** drafts 目录存在未批准 case
- **THEN** eval summary 记录 skipped drafts，score 和通过率仅基于 approved cases

#### Scenario: Provider score fan-out 降级不丢本地证据
- **WHEN** 可选 observability provider 写入失败
- **THEN** eval result 保留 local score/trace refs，并返回脱敏 degraded provider status，不删除或回滚本地证据

#### Scenario: Secret 不进入示例证据
- **WHEN** 输入、provider error 或 tool result 含 token/cookie/password 形状内容
- **THEN** output、trace、score、audit、artifact 摘要和错误信息均不包含原始 secret
