## ADDED Requirements

### Requirement: 工具意图与结构化业务输出使用不同判别合同
Provider-neutral structured output SHALL 继续只表示 Agent 注册输出 schema 下的最终业务结果。工具意图 SHALL 使用独立 `ProviderToolIntentCandidate`、`ToolIntent`、capability、schema identity、错误码和 turn discriminator；系统 MUST NOT 用 structured result 的任意字段、schema 名称或 JSON 内容触发工具 resolve/执行，也 MUST NOT 把工具 intent 当业务成功结果发布。

#### Scenario: 结构化结果包含工具形状仍是业务结果
- **WHEN** Agent output schema 合法允许 `tool_name` 或 `arguments` 字段且 provider 返回匹配 value
- **THEN** 结果继续是 `final_structured` 并按 MOD-005 验证
- **AND** ToolRegistry 与所有 handler 调用计数为零

#### Scenario: 工具候选不能进入业务 structured settlement
- **WHEN** provider 返回 `ProviderToolIntentCandidate`
- **THEN** 核心只能按 tool-intent capability 验证
- **AND** 不生成 MOD-005 valid structured result 或业务 eval success
