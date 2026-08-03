## ADDED Requirements

### Requirement: Agent Registry 冻结可用于模型意图的工具 catalog
Agent Registry SHALL 在全量加载时把 Agent 配置 `tool_allowlist` 逐值投影为 descriptor `tool_policy.allowed_tools`，再与 ToolRegistry descriptors 求有序交集并生成只读 `tool-catalog-v1`；本 change MUST NOT 新增或接受顶层配置字段 `allowed_tools`。Catalog 每项 MUST 绑定 tool name、input schema ref/version/digest、action/resource 和原始 ordinal；未知、重复、非法 schema、配置/descriptor 投影不一致或 action/resource 漂移 MUST 在 executor import、provider client 和模型调用前使全量加载原子失败。业务 executor MUST NOT 取得可变 catalog、handler 或任意注册能力。

#### Scenario: 合法 Agent catalog 稳定重载
- **WHEN** 相同 Agent tool allowlist 与 Registry descriptors 重载
- **THEN** catalog canonical bytes 和 digest 保持相同且顺序与 Agent allowlist 一致

#### Scenario: 配置与 descriptor 投影逐值一致
- **WHEN** Registry 从合法 `tool_allowlist` 装载 Agent descriptor
- **THEN** `tool_policy.allowed_tools` 与配置列表逐项同序且无额外工具
- **AND** 顶层 `allowed_tools` 配置字段以未知字段关闭失败

#### Scenario: 未知或漂移工具阻止 Registry 加载
- **WHEN** Agent 引用未知工具、重复工具或 descriptor schema/action/resource 与冻结身份冲突
- **THEN** Registry 整体加载失败且不暴露部分 Agent/catalog、不构造 provider client

#### Scenario: Bound 入口只取得当前 Agent catalog
- **WHEN** executor 从 `build_execution_context()` 请求 tool-intent model turn
- **THEN** runtime 只解析当前绑定 agent id 的只读 catalog
- **AND** executor payload 不能替换 agent id、catalog definition 或 schema digest
