## ADDED Requirements

### Requirement: Service app 注册 RUN-006 精确契约
service app SHALL 注册 RUN-006，并在 OpenAPI 中精确声明 header/query、`text/event-stream` success content、JSON error envelope 与允许的 response status。route MUST 复用现有 run ownership、tenant、policy、event sink 和 request/trace correlation，不得创建第二套事件存储。

#### Scenario: OpenAPI 包含精确 RUN-006
- **WHEN** 生成 service app OpenAPI
- **THEN** RUN-006 path、parameters、success media type 与错误状态集合和 `API-Contract.md` 一致，不缺失也不暴露额外状态

#### Scenario: 不存在或跨租户 run 不建立 stream
- **WHEN** 调用方请求不存在或不属于当前租户的 run
- **THEN** API 在发送 SSE headers 前返回稳定 404 envelope，不泄漏其他租户事件
