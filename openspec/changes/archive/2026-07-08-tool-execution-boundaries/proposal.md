## 上游依据

- Product-Spec.md: `REQ-011: 工具系统、Shell、File 和 MCP`，以及 AC-025 至 AC-028。
- DEV-PLAN.md: `Phase 8: ToolRegistry、FileTool、ShellTool 与 MCP Client`。
- API-Contract.md: 第 2 节 Tools 信任边界、第 4.7 节数据保护、第 9 节 Tool Execution CLI / Runtime Seam、第 11 节入口 / 调用方映射。
- Design-Brief.md 或设计稿：不适用，本 change 不涉及产品化前端 UI。
- CONTEXT.md / ADR: 未发现本轮必须读取的领域上下文或 ADR。

## 背景与动机

Phase 7 已把 policy、approval、guardrail 和 audit seam 建好，但 runtime 还没有真实工具执行边界。现在需要把 FileTool、ShellTool 和 MCP client 纳入同一套 ToolRegistry、policy、trace/audit 与 context trust contract，否则后续 Retrieval、Observability 和 Eval Gate 会继续绕过安全边界。

## 变更内容

- 新增 `ToolRegistry` 能力，统一本地工具、MCP 工具、schema validation、policy interception、trace/audit 与结构化结果。
- 新增 Workspace FileTool，支持 read/write/list/search/apply_patch/delete，并受 workspace root、`.agentignore` 和 policy 控制。
- 新增 ShellTool，默认 disabled；显式启用后仍受 allowlist/denylist、timeout、环境变量白名单、approval 和输出截断控制。
- 新增 MCP client connector，支持 stdio 与 HTTP/SSE 工具发现和调用；未 allowlist 的 MCP tool 被 policy 拒绝，返回内容默认按 untrusted 处理。
- 基于已扩展的 `API-Contract.md` Phase 8 seam，实现 CLI/runtime/module 工具入口；当前不新增 HTTP route，并用局部 contract tests 防止契约与实现漂移。
- 按 Product-Spec 和 DEV-PLAN 纳入 `workspaces` 与 `tool_invocations` 持久化证据，参数和结果大 payload 走 artifact/ref。

## 非目标

- 不实现 Phase 9 RetrievalProvider、RAG indexing、PGroonga、pgvector 或 citation 行为。
- 不实现 Phase 10 observability provider adapters，只写入现有 event/audit/artifact seam。
- 不实现 Phase 11 eval gate、eval dataset approved 写入或 release automation。
- 不新增 MCP server template；P0 只做 MCP client connector。
- 不物理拆分独立 tool gateway 服务，只保留可拆 interface 和 DTO 边界。

## 能力范围

### 新增能力

- `tool-execution-boundaries`: 统一工具注册、workspace 文件访问、shell 执行、MCP client 调用、输出信任标注和大 payload artifact_ref。

### 修改能力

- `agent-registry-model-context`: ContextAssembler 已定义 tool output 输入语义，本 change 只接入既有 `source_ref`、`trust_level` 和 truncation metadata，不修改其既有 REQUIREMENTS。
- `auth-policy-hitl-approvals`: 默认危险动作策略已覆盖 shell、删除文件、workspace 外访问和 MCP 连接，本 change 复用该 seam，不修改其既有 REQUIREMENTS。
- `canonical-events-artifacts`: artifact store 和 tool event type catalog 已存在，本 change 复用该 seam，不修改其既有 REQUIREMENTS。

## 影响范围

- 代码：新增 `agent_harness.tools`、`agent_harness.mcp`、`agent_harness.adapters.mcp`，扩展 CLI tools group、配置 schema 和 service-app tools config。
- 契约：更新 `API-Contract.md` 的 Phase 8 CLI/runtime seam；新增 OpenSpec delta spec 和 contract tests。
- 数据：新增或扩展 `workspaces` / `tool_invocations` migration、repository/UoW 和 contract tests；tool 参数与结果大 payload 写 artifact/ref。
- 依赖：使用官方 MCP Python SDK adapter；core contracts 和业务入口不得直接 import MCP SDK。
- 安全：所有 tool/MCP output 默认 untrusted，secret、大输出和指令型文本必须先被截断、标源、脱敏或写 artifact_ref。
