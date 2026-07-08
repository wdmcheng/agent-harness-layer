## 上下文

Phase 1-7 已完成 workspace、config、storage/UoW、canonical event/artifact、runtime/checkpoint、agent registry/model/context、auth/policy/HITL approval。Phase 8 的重点不是再造一条执行链路，而是把文件、shell 和 MCP tool 纳入已有 `IdentityContext`、`PolicyEngine`、`AuditService`、`EventBus`、`FileArtifactStore` 和 `ContextAssembler` 边界。

当前 `API-Contract.md` 已补入 Phase 8 Tool Execution CLI / Runtime Seam，声明 tools CLI/runtime/module seam、无新增 HTTP route、`ToolCallRequest`、`ToolCallResult`、`workspaces` 和 `tool_invocations` 持久化要求。Product-Spec 的 REQ-011 要求 FileTool、ShellTool、MCP client 和 tool output trust metadata；DEV-PLAN 指定了 `agent_harness.tools`、`agent_harness.mcp` 和 `adapters.mcp` 的目标文件。

## 目标 / 非目标

**目标：**
- 用 `ToolRegistry` 统一工具 descriptor、schema validation、policy interception、result guard、trace/audit metadata。
- 为 Workspace FileTool、ShellTool 和 MCP client 提供 provider-neutral DTO 和公开 module/CLI 接缝。
- 确保大输出走 artifact_ref，tool/MCP 输出默认带 `source_ref`、`trust_level` 和 truncation metadata。
- 将官方 MCP Python SDK 限定在 adapter 层；核心契约和 template app 不直接依赖 MCP SDK。
- 按上游实体表持久化 `workspaces` 和 `tool_invocations` evidence。

**非目标：**
- 不做 RetrievalProvider、RAG、eval gate、observability SaaS adapter 或 release automation。
- 不新增 HTTP tool execution route；Phase 8 先通过 CLI/runtime/module seam 验证。
- 不把 P0 物理拆成 tool gateway 服务，只保留未来可拆接口。

## 设计决策

1. **ToolRegistry 作为唯一执行入口。**  
   Registry 接受 actor、agent/run/trace metadata、tool name 和 arguments，先校验 descriptor schema，再检查 allowlist 和 policy，最后调用 tool implementation。替代方案是让 FileTool/ShellTool/MCP 分别检查 policy；拒绝，因为会让 audit、approval 和 output guard 证据不一致。

2. **FileTool 与 ShellTool 只依赖 workspace policy，不依赖 API 层。**  
   Workspace path guard 负责 root 解析、`.agentignore`、路径摘要和操作类别；API/CLI/runtime 只传入 actor 和 metadata。这样未来拆 tool gateway 时不需要重写文件安全规则。

3. **ShellTool 默认 disabled，启用后仍不是“裸 subprocess”。**  
   Shell 执行需要 allowlist/denylist、env whitelist、timeout、cwd sandbox、stdout/stderr artifact_ref 和 policy check。替代方案是直接暴露 shell command callback；拒绝，因为它绕过 Phase 7 HITL 和 audit。

4. **MCP SDK 只在 `adapters.mcp.python_sdk`。**  
   Context7 当前官方文档显示 Python client 使用 `ClientSession`、`stdio_client`、`streamable_http_client`、`list_tools()` 和 `call_tool()`。本 change 的 core/mcp interface 使用自定义 DTO，adapter 负责转换官方 SDK session 和 result。

5. **补 `workspaces` 和 `tool_invocations` 表，但大内容仍走 artifact。**  
   Product-Spec 和 DEV-PLAN 已把这两个实体放入 Phase 8。表只保存 root/policy、tool name、args_ref、result_ref、status、duration 和关联 metadata；完整 arguments/result/stdout/stderr/file/MCP payload 继续由 artifact store 承载，避免数据库和 audit/event 被大文本撑爆。

6. **API-Contract 记录“无 HTTP route”的 Phase 8 边界。**  
   本阶段新增 CLI/runtime seam，不新增 `/api/v1/tools`。contract tests 要验证 OpenAPI 没有伪 route，同时验证 CLI 和 module seam 满足 REQ-011。

## 影响面

- `packages/agent-harness/src/agent_harness/tools/`: registry、schema、workspace、file_tool、shell_tool、output_guard。
- `packages/agent-harness/src/agent_harness/mcp/`: provider-neutral MCP client DTO/interface。
- `packages/agent-harness/src/agent_harness/adapters/mcp/python_sdk.py`: 官方 MCP Python SDK adapter。
- `packages/agent-harness/src/agent_harness/config/schemas.py`: tool/workspace/shell/MCP settings。
- `packages/agent-harness/src/agent_harness/storage/`: `workspaces` / `tool_invocations` models、migration、repository 和 UoW seam。
- `packages/agent-harness/src/agent_harness/cli.py`: `agent-harness tools list/call` CLI 接缝。
- `templates/service-app/configs/tools.yaml`: 模板工具默认值和 MCP server 配置示例。
- `API-Contract.md`: Phase 8 入口 / 调用方映射与验证规则。
- 测试：tool registry、file、shell、MCP、output guard、API contract 的 contract tests；MCP SDK 隔离的 import-boundary tests。

## 测试接缝

- Module 接缝：用 fake tools 实例化 `ToolRegistry`，断言 schema、policy、audit 和 output 行为。
- Workspace 接缝：在临时 workspace 与 `.agentignore` 下运行 FileTool，断言外部路径 / ignored path denial 和允许路径的 read/search/write/patch/delete。
- Shell 接缝：在隔离临时 workspace 中覆盖 ShellTool disabled、denied、timeout 和长输出场景。
- MCP 接缝：用 fake MCP adapter 覆盖 discovery、call allowlist 和 untrusted output；SDK adapter import 隔离单独测试，不要求真实 server 凭据。
- CLI 接缝：用 local profile 和临时 workspace 运行 `agent-harness tools list/call`，检查 stdout/stderr/exit code。
- Contract 接缝：检查 `API-Contract.md` 包含 Phase 8 映射，并证明 service OpenAPI 不暴露未记录的 `/api/v1/tools` route。
- Persistence 接缝：运行 SQLite repository/migration tests 覆盖 `workspaces` 和 `tool_invocations`；service smoke 单独证明 PostgreSQL migration 包含同样表。

## 风险 / 取舍

- [风险] Tool invocation 表被误用来存完整 payload。→ 缓解：repository 和 tests 只允许 refs 与摘要入库，大内容写 artifact。
- [风险] Shell tests 误触开发者机器状态。→ 缓解：所有 shell/file tests 使用临时 workspace、env whitelist，且不继承 secret。
- [风险] MCP SDK API 变化。→ 缓解：SDK import 只留在 adapter，并用 provider-neutral fake seam 覆盖核心行为；依赖范围已固定为 `<2`。
- [风险] `.agentignore` 语义膨胀。→ 缓解：实现足够保护 workspace safety 的保守 gitignore-style 匹配；未支持边界写入配置说明。

## 迁移计划

新增 Alembic migration 创建 `workspaces` 和 `tool_invocations`。既有 local/service profiles 默认仍不启用危险工具。回滚代码层面是移除新 tool modules/config/CLI group；若 migration 已应用，需要按后续 archive/release 策略新增反向 migration，而不是手工删表。

## 待确认问题

- 是否在 Phase 12 示例 agent 中暴露 tool 使用体验；本 change 不处理示例 agent 产品流。
