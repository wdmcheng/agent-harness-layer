## ADDED Requirements

### Requirement: RUN-002 原子切换为 durable delegation detail
service app SHALL 在真实 delegation aggregation 可读的同一 change 中把 `GET /api/v1/runs/{run_id}` 从 `RunCreateResponse` 切换为 API Contract 5.31 的 `RunDetailResponse`。响应 MUST 包含当前 agent、nullable parent run、`DelegationSummary` 或 null；根 run 和 child run 均从持久化关系构造，不得用空占位伪装完成。

#### Scenario: Parent run 返回 durable aggregation
- **WHEN** parent run 已有完成或失败的 child delegation evidence
- **THEN** RUN-002 返回 `RunDetailResponse`，其中 delegation summary 与持久化 child status、`ModelUsageEvidence` 和 trace refs 对账一致

#### Scenario: Child run 返回 parent ref
- **WHEN** 调用方读取 delegated child run
- **THEN** RUN-002 返回该 child 的 `parent_run_id`，且不会泄漏其他租户关系

#### Scenario: OpenAPI 原子切换 schema
- **WHEN** 生成 service app OpenAPI
- **THEN** RUN-002 success 只引用 `RunDetailResponse`，状态与 error envelope 保持 API Contract 精确集合，不再引用 `RunCreateResponse`

### Requirement: DLG-001 不新增公开 HTTP route
P0 DLG-001 SHALL 只通过 runtime/worker 注册的内置 `agent.delegate` tool/module seam 调用。service app OpenAPI MUST NOT 暴露 `/delegations` endpoint；授权、错误和结果使用 tool/module DTO 与 `DelegationSummary`。

#### Scenario: OpenAPI 没有 delegation route
- **WHEN** 生成 service app OpenAPI
- **THEN** 不存在 `/api/v1/runs/{parent_run_id}/delegations` 或其他公开 delegation path，RUN-002 是唯一新增公开读取形状
