## 1. 契约与公开 seam

- [x] 1.1 扩展 `API-Contract.md` Phase 8 入口映射，明确 tools CLI/runtime/module seam、无新增 HTTP route、policy/audit/artifact/context 规则和局部验证要求。
- [x] 1.2 新增 Phase 8 contract tests，覆盖 API-Contract 条目、稳定工具错误码、OpenAPI 无未声明 `/api/v1/tools` route、CLI/runtime seam 基线和 MCP SDK import boundary。

## 2. ToolRegistry 与输出边界

- [x] 2.1 新增 `agent_harness.tools` DTO、schema validation 和 `ToolRegistry`，公开 seam 覆盖未知工具、非法 arguments、policy deny/require_approval 和 audit/event metadata。
- [x] 2.2 新增 output guard，覆盖大 payload artifact_ref、secret redaction、指令型文本 injection summary、`source_ref`、`trust_level` 和 truncation metadata。

## 3. Workspace FileTool

- [x] 3.1 实现 workspace root guard 和 `.agentignore` 处理，公开 seam 覆盖 workspace 外路径与 ignored path denial。
- [x] 3.2 实现 `read_file`、`write_file`、`list_files`、`search_files`、`apply_patch`、`delete_file`，公开 seam 覆盖允许路径、危险操作 policy、结果 metadata 和无越界副作用。

## 4. ShellTool

- [x] 4.1 实现 ShellTool 默认 disabled、显式启用、command allowlist/denylist、env whitelist、workspace cwd 与路径参数边界和 timeout。
- [x] 4.2 覆盖 shell 长 stdout/stderr 截断与 artifact_ref，确保 event/audit/result 不内联完整大文本。

## 5. MCP client 连接器

- [x] 5.1 新增 provider-neutral MCP client interface 和 fake adapter tests，覆盖 discovery、allowlist、policy denial 和 untrusted result DTO。
- [x] 5.2 新增 official MCP Python SDK adapter，支持 stdio 与 HTTP/SSE 的 session 初始化、`list_tools()` 和 `call_tool()`，并保持 vendor import 只在 adapter 层。

## 6. 持久化、CLI、配置与收口验证

- [x] 6.1 新增 `workspaces` 和 `tool_invocations` migration、models、repository/UoW seam，并用 SQLite repository contract tests 验证 refs/status/duration/tenant-run-agent-trace 关联。
- [x] 6.2 扩展 config schema 和 `templates/service-app/configs/tools.yaml`，提供 workspace、shell、MCP server、allowlist/denylist 和 output limit 默认值说明。
- [x] 6.3 增加 `agent-harness tools list/call` CLI 接缝，使用同一 ToolRegistry、policy、artifact、持久化 repository 和 output guard，并通过 contract tests 验证。
- [x] 6.4 跑 `openspec validate tool-execution-boundaries --type change --strict`、Phase 8 局部 tests、`make quality`、`make test`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files`；单独列出 SQLite 与 PostgreSQL/Redis 中 `workspaces`、`tool_invocations` 的验证证据。
- [x] 6.5 通过 code-reviewer Stage 1/2 后更新 DEV-PLAN 最终状态，并写入 `.agents/.needs-review=clean`。
- [x] 6.6 本地提交 Phase 8 实现与 OpenSpec change。
