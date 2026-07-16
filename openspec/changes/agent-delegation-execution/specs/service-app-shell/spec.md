## ADDED Requirements

### Requirement: RUN-002 原子切换为 durable delegation detail
service app SHALL 在真实 delegation aggregation 可读的同一 change 中把 `GET /api/v1/runs/{run_id}` 从 `RunCreateResponse` 切换为 API Contract 5.31 的 `RunDetailResponse`。响应 MUST 包含当前 agent、nullable parent run、`DelegationSummary` 或 null；根 run 和 child run 均从持久化关系构造，不得用空占位伪装完成。`DelegationSummary.children` MUST 以带 `child_run_id` 的 durable parent-child relation 决定 membership，并从持久化 child run 取得 `RunStatus`；aggregate row 只补充已结算 evidence，MUST NOT 决定 child 是否存在。仅活动 child 或已终态但尚未聚合的 child MUST 以 unknown 数值出现并令 `budget_status=incomplete`；已结算与未结算 child 并存时 MUST 全部返回；当且仅当确无 child relation 时 summary 才为 null。

#### Scenario: Parent run 返回 durable aggregation
- **WHEN** parent run 已有完成或失败的 child delegation evidence
- **THEN** RUN-002 返回 `RunDetailResponse`，其中 delegation summary 与持久化 child status、`ModelUsageEvidence` 和 trace refs 对账一致

#### Scenario: 仅活动 child 仍出现在 parent detail
- **WHEN** parent 已持久化 child relation，且 child `RunStatus` 为 `created|running|waiting`、尚无 terminal aggregate
- **THEN** RUN-002 返回包含该 child 身份、持久化状态与 trace refs 的非 null summary，token/cost/latency 为 null 且 `budget_status=incomplete`

#### Scenario: 已终态但尚未聚合的 child 不被遗漏
- **WHEN** child 已是 `completed|failed|cancelled`，但可重入 aggregation 尚未写入 aggregate row
- **THEN** RUN-002 仍按 durable relation 返回该 child，未结算数值为 null 且 `budget_status=incomplete`，不得返回 null

#### Scenario: 已结算与未结算 child 并存
- **WHEN** parent 同时存在已有可信 aggregate 的 child 与活动或尚未聚合的 child
- **THEN** RUN-002 的 `children` 包含全部 durable relation，只累计已知 token，cost/latency 按全体完整性返回 null，且 `budget_status=incomplete`

#### Scenario: 确无 child relation 时 summary 为 null
- **WHEN** parent 不存在任何带 `child_run_id` 的 durable delegation relation
- **THEN** RUN-002 返回 `delegation_summary=null`，不得以空 children 对象伪装已有 aggregation

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
