## ADDED Requirements

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
系统 SHALL 在 Phase 8 持久化 `workspaces` 和 `tool_invocations` evidence。Workspace 记录 SHALL 包含 tenant、可选 run/agent 关联、workspace root 和 policy ref；ToolInvocation 记录 SHALL 包含 tenant、agent、可选 run、tool name、args_ref、result_ref、status、duration、trace/request metadata 和错误摘要。工具大参数和结果 MUST 通过 artifact/ref 关联，不直接塞入记录正文。

#### Scenario: local migration 创建工具表
- **WHEN** developer 使用 local profile 执行 migration
- **THEN** SQLite schema 包含 `workspaces` 和 `tool_invocations` 表，且 repository 可写入并读取 workspace 与 tool invocation evidence

#### Scenario: service migration 创建工具表
- **WHEN** developer 使用 service profile 执行 migration
- **THEN** PostgreSQL schema 包含同一批工具 evidence 表，并保持与 SQLite repository contract 一致

#### Scenario: invocation 记录不保存完整大 payload
- **WHEN** 工具 arguments、stdout、stderr、file content 或 MCP response 超过 inline 上限
- **THEN** `tool_invocations` 只保存 `args_ref` / `result_ref` 和状态摘要，完整内容由 artifact store 按 ref 读取

### Requirement: Phase 8 入口契约先于实现扩展
`API-Contract.md` SHALL 在 Phase 8 实现前说明 tool/file/shell/MCP 的 CLI/runtime/module seam、认证与 policy 关系、输入/输出 DTO、错误码、安全规则和验证要求。若本 change 不新增 HTTP endpoint，文档 MUST 明确当前无新增 route，并用 contract tests 覆盖 CLI/runtime seam 与 OpenAPI 无漂移。

#### Scenario: API-Contract 包含 Phase 8 seam
- **WHEN** 维护者阅读 `API-Contract.md`
- **THEN** 能识别 `agent-harness tools list`、`agent-harness tools call` 或等价 runtime seam 如何映射 ToolRegistry、PolicyEngine、artifact store 和 ContextAssembler

#### Scenario: Contract tests 防止工具契约漂移
- **WHEN** Phase 8 contract tests 运行
- **THEN** tests 同时检查 `API-Contract.md` 条目、CLI/runtime behavior、policy denial、artifact_ref、trust metadata 和未新增 HTTP route 的 OpenAPI 预期

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
