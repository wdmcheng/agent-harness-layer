## MODIFIED Requirements

### Requirement: P0 HTTP 契约与运行时 OpenAPI 无漂移
service-app SHALL 使用统一 DTO 和 `ApiErrorEnvelope` 暴露 `API-Contract.md` 当前定义的 P0 endpoint；实现前 MUST 先为 health、单项 approval 读取和 `EVL-001` 至 `EVL-003` 的每个 operation 补齐字段级契约基线。所有 response MUST 携带适用的 `request_id`，run response MUST 携带 `run_id`，validation error MUST 使用 422 `ApiErrorEnvelope`。RUN-001 至 RUN-005 的运行时 OpenAPI response status 集合 MUST 与 `API-Contract.md` 逐 operation 精确相等，不得因 router 级 metadata 暴露生产路径不可能返回的 status；每个已声明错误 status MUST 引用 `ApiErrorEnvelope`。本变更中的 RUN-002 MUST 继续使用 `RunCreateResponse`，不得提前暴露后续 delegation change 才提供的 `RunDetailResponse`。

#### Scenario: OpenAPI 全量漂移检查通过
- **WHEN** contract test 将运行时 OpenAPI 与 `API-Contract.md` 当前 P0 path、method、schema、认证和错误响应对照
- **THEN** 所有已定义 endpoint 均有一致声明，且不存在缺失、额外或字段语义漂移

#### Scenario: 所有适用 operation 的 validation error 使用统一 envelope
- **WHEN** contract test 参数化遍历 API Contract 明确声明 422 的当前 P0 operations 并提交各自无效输入
- **THEN** 每个适用 operation 都返回 HTTP 422 和 `ApiErrorEnvelope`，其中含脱敏错误和 `request_id`，而不是 FastAPI 默认 detail body

#### Scenario: RUN-001 的 OpenAPI response 精确
- **WHEN** contract test 读取 `POST /api/v1/agents/{agent_id}/runs` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`202`、`400`、`401`、`403`、`404`、`409`、`422`、`500`、`503`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: RUN-002 的当前 detail schema 与 response 精确
- **WHEN** contract test 读取 `GET /api/v1/runs/{run_id}` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`500`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`，且 schema 不包含 `RunDetailResponse`

#### Scenario: RUN-003 的 OpenAPI response 精确
- **WHEN** contract test 读取 `GET /api/v1/runs/{run_id}/events` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`422`、`500`，成功 response 引用 `RunEventsResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: RUN-004 的 OpenAPI response 精确
- **WHEN** contract test 读取 `POST /api/v1/runs/{run_id}/cancel` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`409`、`500`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: RUN-005 的 OpenAPI response 精确
- **WHEN** contract test 读取 `POST /api/v1/runs/{run_id}/resume` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`409`、`422`、`500`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: Run response 漂移检查同时拒绝缺失和多余 status
- **WHEN** 任一 RUN-001 至 RUN-005 operation 的运行时 OpenAPI 相对精确基准缺失一个 status 或额外出现一个 status
- **THEN** contract test 必须失败，并定位发生漂移的 path、method、缺失集合与多余集合
