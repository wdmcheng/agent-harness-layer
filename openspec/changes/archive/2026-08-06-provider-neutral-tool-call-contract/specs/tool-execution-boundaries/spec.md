## ADDED Requirements

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
