# tool-execution-boundaries Specification

## Purpose
定义工具执行边界的长期契约：ToolRegistry、Workspace FileTool、ShellTool、MCP client、输出信任元数据和工具调用 evidence 必须通过统一的 schema、policy、audit、artifact 与 workspace 规则收口。
## Requirements
### Requirement: ToolRegistry 统一工具执行边界
系统 SHALL 暴露 `ToolRegistry`，用于注册本地工具和 MCP 工具，并在执行前统一完成工具名解析、输入 schema validation、agent/tool allowlist、policy check、trace/audit metadata 生成和稳定错误输出。Registry MUST NOT 暴露 callable、vendor SDK object、绝对路径或 provider 原始响应给 API、CLI、runtime 或业务 agent。工具错误码 MUST 使用 `API-Contract.md` 第 5.21 节定义的稳定 code。

#### Scenario: 未注册工具被拒绝
- **WHEN** 调用方请求执行未注册的 tool name
- **THEN** registry 返回 `tool.not_found`，不执行任何工具实现，并写入可追踪 audit/event 摘要

#### Scenario: 输入 schema validation 先于执行
- **WHEN** 调用方传入不符合工具 input schema 的 arguments
- **THEN** registry 在调用工具实现前返回 `tool.schema_validation_failed`，错误包含字段路径，且不产生文件、shell、MCP 或外部副作用

#### Scenario: policy denial 阻断工具执行
- **WHEN** `PolicyEngine` 对 tool action 返回 `deny`
- **THEN** registry 不执行目标工具，返回 `tool.policy_denied`，并写入 action、resource、tenant、user、agent、run 和 trace 摘要

### Requirement: Approved tool continuation 使用绑定 grant 和持久化单次执行 claim
`ToolRegistry` SHALL 提供只供 approval resume 使用的受控调用 seam，接收已由 `ApprovalService` 验证的 `ApprovalGrant`。grant MUST 绑定 approval、tenant、identity、agent、run、tool action/resource 和 arguments hash；registry MUST 在调用 handler 前创建以 `approval_id` 唯一的持久化 execution claim，完成后保存 result ref。

#### Scenario: 匹配 grant 执行一次并保存结果
- **WHEN** approved continuation 的 grant 与 checkpoint/tool request 全部字段匹配且不存在 execution claim
- **THEN** registry 先持久化 claim，再调用 handler 一次，保存 completed result ref 和 audit/trace evidence

#### Scenario: 不匹配 grant 被拒绝
- **WHEN** approval、tenant、identity、agent、run、action、resource 或 arguments hash 任一不匹配
- **THEN** registry 返回稳定 approval/policy denial，handler 执行计数为零，不创建 completed invocation

#### Scenario: 已完成 claim 返回既有结果
- **WHEN** 相同 `approval_id` 的 completed execution claim 再次进入 resume seam
- **THEN** registry 返回已持久化 result ref，不再次调用 handler

#### Scenario: 未完成 claim 不自动重放副作用
- **WHEN** 相同 `approval_id` 存在 `executing` claim 但没有 completed result ref
- **THEN** registry 返回 `needs_review`/稳定中断状态并保留证据，不自动重试 handler

### Requirement: Workspace FileTool 只访问受控 workspace
系统 SHALL 提供 Workspace FileTool，支持 `read_file`、`write_file`、`list_files`、`search_files`、`apply_patch` 和 `delete_file`。FileTool MUST 将所有相对路径解析到配置的 workspace root 内，遵守 `.agentignore`，并对 workspace 外访问、删除文件、批量写入或 patch 操作执行 policy check。

#### Scenario: workspace 外路径被拒绝或要求审批
- **WHEN** 调用方请求 FileTool 读取、写入、搜索、patch 或删除 workspace root 外的路径
- **THEN** FileTool 默认返回 `deny` 或 `require_approval`，且不读取或修改目标路径

#### Scenario: agentignore 阻断受忽略路径
- **WHEN** `.agentignore` 匹配某个 workspace 内路径
- **THEN** FileTool 对该路径的读取、搜索、写入、patch 或删除返回稳定 denial，不泄漏文件内容

#### Scenario: 文件操作输出保留 source metadata
- **WHEN** FileTool 成功返回文件内容、搜索结果或 patch/delete 结果
- **THEN** 输出包含 `source_ref`、`trust_level`、操作摘要和 truncation metadata，供 ContextAssembler 或 audit seam 使用

### Requirement: ShellTool 默认关闭且受强约束
系统 SHALL 提供 ShellTool，但默认 disabled。只有显式启用后才可执行命令；执行时 MUST 应用 command allowlist/denylist、workspace sandbox、timeout、环境变量白名单、policy check、stdout/stderr 截断和 artifact_ref 写入规则。

#### Scenario: 默认 disabled 阻断 shell
- **WHEN** 未显式启用 ShellTool 的调用方请求 `shell.execute`
- **THEN** 系统返回 disabled denial，不启动子进程，也不读取调用方环境变量

#### Scenario: allowlist 和 timeout 控制命令执行
- **WHEN** ShellTool 已启用但命令不在 allowlist、命中 denylist 或超过 timeout
- **THEN** 系统拒绝或终止命令，并返回结构化结果，包含 `exit_code`、`duration_ms`、`timed_out` 和截断摘要

#### Scenario: 长输出写入 artifact_ref
- **WHEN** shell stdout 或 stderr 超过 inline 上限
- **THEN** 结果只内联截断摘要，并在 `ToolCallResult.result.stdout_ref` 或 `ToolCallResult.result.stderr_ref` 中提供 artifact ref，event/audit payload 不包含完整大文本

### Requirement: MCP client 通过 adapter 隔离 vendor SDK
系统 SHALL 提供 MCP client connector，支持配置 stdio 与 HTTP/SSE server、tool discovery、MCP tool allowlist、调用前 policy check、调用结果 trace/audit 和输出 trust 标注。官方 MCP Python SDK 只能出现在 `agent_harness.adapters.mcp` 这类 adapter seam，core contracts、runtime、template app 和业务 agent MUST NOT 直接 import MCP SDK。

#### Scenario: 未 allowlist 的 MCP tool 被拒绝
- **WHEN** MCP server discovery 返回某个 tool，但该 tool 未在允许清单中
- **THEN** registry 或 MCP connector 对调用返回 policy denial，且不向 MCP server 发送 tool call

#### Scenario: MCP stdio 和 HTTP/SSE 使用统一 DTO
- **WHEN** 调用方通过 stdio 或 HTTP/SSE MCP server 发现和调用工具
- **THEN** 返回的 tool descriptor 和 invocation result 使用 provider-neutral DTO，不暴露 MCP SDK session、transport 或 wire object

#### Scenario: MCP 输出默认 untrusted
- **WHEN** MCP tool 返回文本、JSON、resource 或其他内容
- **THEN** 系统将输出标为 `trust_level="untrusted"`，保留 `source_ref`、server/tool 标识、truncation metadata 和 artifact_ref

### Requirement: Tool output 进入上下文前保留信任边界
工具和 MCP 输出 SHALL 先经过 output guard，再进入 ContextAssembler 或 event/audit seam。output guard MUST 对大 payload 截断并写 artifact_ref，对 secret-like 字段脱敏，对指令型文本保留 untrusted/source metadata，且不得让 tool/MCP 文本覆盖 system、policy 或 developer 指令。

#### Scenario: 指令型 tool output 不覆盖高优先级指令
- **WHEN** tool 或 MCP output 包含类似 system override、policy bypass 或 developer instruction 的文本
- **THEN** output guard 保留原文来源和 untrusted 标记，生成 injection summary，并让 ContextAssembler 只把它作为引用内容处理

#### Scenario: secret 不进入 event 和 audit
- **WHEN** tool result、MCP response、shell output 或 file content 包含 token、password、cookie 或 secret-like 字段
- **THEN** event payload、audit payload 和 inline result 只包含脱敏摘要或 artifact_ref，不包含原始 secret value

#### Scenario: ContextAssembler 接收 tool metadata
- **WHEN** tool/MCP output 被加入上下文输入
- **THEN** ContextAssembler input 包含 `source_ref`、`trust_level`、token estimate、truncation metadata 和 artifact_ref，而不是裸字符串拼接

### Requirement: Workspace 和 ToolInvocation evidence 可持久化
系统 SHALL 持久化 `workspaces` 和 `tool_invocations` evidence。Workspace 记录 SHALL 包含 tenant、可选 run/agent 关联、workspace root 和 policy ref；ToolInvocation 记录 SHALL 包含 tenant、agent、可选 run、tool name、args_ref、result_ref、status、duration、trace/request metadata 和错误摘要。工具大参数和结果 MUST 通过 artifact/ref 关联，不直接塞入记录正文。

#### Scenario: local migration 创建工具表
- **WHEN** developer 使用 local profile 执行 migration
- **THEN** SQLite schema 包含 `workspaces` 和 `tool_invocations` 表，且 repository 可写入并读取 workspace 与 tool invocation evidence

#### Scenario: service migration 创建工具表
- **WHEN** developer 使用 service profile 执行 migration
- **THEN** PostgreSQL schema 包含同一批工具 evidence 表，并保持与 SQLite repository contract 一致

#### Scenario: invocation 记录不保存完整大 payload
- **WHEN** 工具 arguments、stdout、stderr、file content 或 MCP response 超过 inline 上限
- **THEN** `tool_invocations` 只保存 `args_ref` / `result_ref` 和状态摘要，完整内容由 artifact store 按 ref 读取

### Requirement: 工具入口契约先于实现扩展
`API-Contract.md` SHALL 在工具入口实现前说明 tool/file/shell/MCP 的 CLI/runtime/module seam、认证与 policy 关系、输入/输出 DTO、错误码、安全规则和验证要求。若当前能力不新增 HTTP endpoint，文档 MUST 明确无新增 route，并用 contract tests 覆盖 CLI/runtime seam 与 OpenAPI 无漂移。

#### Scenario: API-Contract 包含工具执行 seam
- **WHEN** 维护者阅读 `API-Contract.md`
- **THEN** 能识别 `agent-harness tools list`、`agent-harness tools call` 或等价 runtime seam 如何映射 ToolRegistry、PolicyEngine、artifact store 和 ContextAssembler

#### Scenario: Contract tests 防止工具契约漂移
- **WHEN** tool execution contract tests 运行
- **THEN** tests 同时检查 `API-Contract.md` 条目、CLI/runtime behavior、policy denial、artifact_ref、trust metadata 和未新增 HTTP route 的 OpenAPI 预期

### Requirement: ToolRegistry 提供无工具副作用的意图解析 seam
`ToolRegistry` SHALL 提供只读 `resolve_intent` 或等价公共 seam，输入绑定的 `ToolIntent` 与 catalog identity，完成 tool name、Agent allowlist、input schema、arguments、schema/source/catalog identity 校验，返回不含 callable、handler、SDK object 或基础设施 client 的 `ResolvedToolIntent`。Resolve MUST NOT 执行 preflight、policy、handler、FileTool、ShellTool、MCP、network 或 artifact materialization；允许的 validation/audit evidence MUST 脱敏。

#### Scenario: 合法 intent 只返回解析结果
- **WHEN** tool name、arguments、Agent allowlist 和 catalog/schema identity 全部匹配
- **THEN** Registry 返回绑定 action/resource/schema 的 `ResolvedToolIntent`
- **AND** 所有工具和外部副作用计数为零

#### Scenario: 未知或无效 intent 零副作用拒绝
- **WHEN** tool 未注册、未授权、arguments schema 无效或 catalog/source identity 漂移
- **THEN** Registry 返回稳定 `tool.*` 或 `model.tool_*` 错误并写脱敏摘要
- **AND** policy、claim、handler、MCP、shell、file 和 network 计数均为零

### Requirement: 工具执行 seam 防御性重验已解析意图
后续 `ToolRegistry.call` 与 `call_approved` 接收模型驱动 intent 时 SHALL 在 execution claim/handler 前重新校验 tool name、arguments digest、schema/catalog、Agent allowlist 与 action/resource 绑定。Resolve 结果 MUST NOT 作为可调用 capability 或绕过现有 policy/HITL/output guard 的授权票据。

#### Scenario: Resolve 后 Registry 漂移阻止执行
- **WHEN** intent resolve 后、执行前 tool descriptor、allowlist、schema 或 action/resource 发生漂移
- **THEN** 执行 seam 在 claim 和 handler 前关闭失败
- **AND** 不使用旧 resolve DTO 取得 handler

### Requirement: 模型驱动工具调用复用统一执行与输出边界
模型工具循环 SHALL 只通过 `ToolRegistry` 的 resolve、`call` 与 `call_approved` seam 执行工具。Registry SHALL 在 execution claim/handler 前重验 intent/canonical arguments/schema/catalog/Agent allowlist/action/resource，复用既有 PolicyEngine、audit、workspace、MCP、artifact、secret redaction 和 output guard。Loop 或 adapter MUST NOT 直接持有 handler、BuiltinTool、MCP client 或 workspace implementation。

#### Scenario: Allow 路径仍经过 Registry 全部门禁
- **WHEN** 模型 intent 合法且 policy allow
- **THEN** handler 只由 Registry 在 schema/allowlist/policy/capacity/claim 成功后调用
- **AND** 结果使用既有 `ToolCallResult` 和 output guard

#### Scenario: Loop 不能调用 Registry 私有 handler
- **WHEN** loop 或 adapter 尝试从 resolve DTO、descriptor 或 service mapping取得 callable
- **THEN** 公共 DTO/类型边界拒绝该对象且工具执行为零

### Requirement: Approved 工具恢复绑定原模型意图
`call_approved` SHALL 要求 `ApprovalGrant`、checkpoint、ToolIntent、ResolvedToolIntent 与当前 bound context 对 loop id、turn ordinal、tool call id、tool name、arguments digest、schema/catalog、action/resource、tenant/identity/run/agent/request/trace逐值一致，并保留既有 approval-id唯一 claim。任一字段缺失、额外、漂移、跨 run 或 lease 失效 MUST 在 handler 前拒绝。

#### Scenario: Matching grant执行一次
- **WHEN** durable approval/checkpoint与当前intent、active grant/lease全部匹配且没有completed claim
- **THEN** Registry先建立或取得唯一claim再执行handler一次并保存result ref

#### Scenario: 同步篡改record与checkpoint仍失败
- **WHEN** caller同步改写approval record和checkpoint中的tool/arguments/catalog identity但与原canonical intent不一致
- **THEN** Registry从受信loop state重算后拒绝且handler计数为零

### Requirement: 所有模型驱动工具调用使用tool-call唯一执行claim
普通和approved模型工具调用 SHALL在handler前以受信派生的`tool_call_id`建立唯一durable invocation claim，并绑定tenant/run/agent/trace、loop/turn、tool name、arguments/schema/catalog/action/resource digests和nullable approval identity。新模型驱动claim还 SHALL保存 `execution_lease_digest`、正整数 `execution_fence`、`execution_lease_expires_at`、nullable `handler_started_at` 与 `not_started_proof_json`。Approved调用 MUST同时满足既有`approval_id`唯一约束，且两个identity只能指向同一row。首次创建只能进入`claimed`；只有持有matching active lease/fence且已确认`claimed→executing`提交的owner MAY取得进程内`ToolExecutionPermit`并调用handler。

#### Scenario: 两worker竞争普通工具
- **WHEN** 两个worker并发执行同一tool_call_id
- **THEN** 只有一个claim winner调用handler，另一方读取exact state且调用计数不增加

#### Scenario: Approved identity不能创建第二row
- **WHEN** 相同approved intent分别按approval_id和tool_call_id竞争
- **THEN** 两个unique identity解析到同一invocation row和一次handler执行

#### Scenario: Binding冲突零handler调用
- **WHEN** 相同tool_call_id携带不同arguments/schema/catalog/action/resource或run identity
- **THEN** Registry返回replay conflict且不覆盖persisted claim

#### Scenario: Claimed lease换租后旧owner失效
- **WHEN** `claimed` row的原lease过期且新worker以CAS轮换lease digest、递增fence并保存可信未开始proof
- **THEN** 只有新lease/fence可以提交`claimed→executing`
- **AND** 旧owner在handler边界前被拒绝且调用计数为零

### Requirement: 工具claim状态决定唯一重放语义
新模型驱动tool invocation的execution state SHALL为`claimed|executing|completed|failed|needs_review`封闭联合。`claimed`表示尚未铸造执行许可；existing claimed只能在原lease过期后，以owner UoW/CAS原子保存`tool-handler-not-started-v1`、轮换lease digest并递增fence后继续。该proof exact fields为`schema_version/tool_call_id/binding_digest/prior_fence/next_fence/previous_lease_expires_at/reason/proof_digest`，`reason`只允许`claim_lease_expired`，digest必须从canonical preimage重算。只有matching active lease/fence可提交`claimed→executing`；提交确认后才铸造一次性`ToolExecutionPermit`，并在调用handler前再次核对permit与row identity。Completed exact replay SHALL返回既有result ref；确定failed exact replay SHALL返回既有稳定错误；executing、`claimed→executing`提交确认未知、handler可能已开始、result persistence/commit acknowledgement未知或字段矛盾 MUST单调提升needs-review。系统 MUST NOT把executing降回claimed、自动重调handler、把unknown改failed或actual-zero。

#### Scenario: Completed result只返回既有ref
- **WHEN**相同tool_call_id的completed claim再次进入call/call_approved
- **THEN** Registry返回persisted ToolCallResult/result ref且handler计数不增加

#### Scenario: Handler后提交确认未知
- **WHEN**handler返回但result/claim commit acknowledgement未知
- **THEN**claim和loop进入needs-review并保留event/capacity围栏
- **AND**恢复不再次调用handler

#### Scenario: Handler前可信失败可确定收口
- **WHEN**claim已创建但durable owner证明handler尚未取得执行权且preflight确定失败
- **THEN**同一claim可收口为failed并保存稳定错误
- **AND**不得创建新的tool_call_id重试

#### Scenario: Executing没有可信未开始旁路
- **WHEN** durable state为`executing`且没有completed/failed exact result
- **THEN** recovery进入`needs_review`并保留claim、lease、预算与event capacity
- **AND** 不接受超时lease、空`handler_started_at`或caller flag把它降回`claimed`

#### Scenario: Claimed到executing提交确认未知
- **WHEN** owner无法确认`claimed→executing`事务是否提交
- **THEN** 当前进程不得铸造`ToolExecutionPermit`或调用handler
- **AND** recovery读取实际durable state；`claimed`只能在lease过期后按proof换租，`executing`进入needs-review
