## ADDED Requirements

### Requirement: 路由和预算决策进入统一 usage evidence
`ModelRouter` SHALL 把实际选择的 provider/model、route/fallback 与 budget decision 传给受控 evidence seam；model/embedding adapter SHALL 返回可归一化的 usage 输入。业务 agent、template agent 和 API route MUST NOT 解析 provider raw event、导入 provider client 或手工填充 `ModelUsageEvidence`。

#### Scenario: Fallback decision 与实际调用一致
- **WHEN** router 因默认模型不可用或预算选择 fallback model
- **THEN** terminal usage evidence 同时记录原 route decision、实际 provider/model 和 budget impact，且字段来自 router/adapter 边界

#### Scenario: 业务 agent 不拼接 raw usage
- **WHEN** import/static boundary 扫描业务 agent、template agent 和 API route
- **THEN** 这些表面不导入 provider usage object/client，也不创建或修改 `ModelUsageEvidence`
