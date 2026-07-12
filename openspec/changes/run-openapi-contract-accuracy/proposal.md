## Source Links

- Product-Spec.md: `REQ-008: API、CLI 与管理面`、`AC-017`；P0 API 管理面与统一错误响应要求。
- DEV-PLAN.md: `Phase 13.5: Run OpenAPI Response / Status 准确性`；Phase 13.8 与 Phase 13.9 的后续边界。
- API-Contract.md: `RUN-001` 至 `RUN-005` 的 method、path、成功/错误 status 与 response schema；`RunCreateResponse` 和未来 `RunDetailResponse` 定义。
- Design-Brief.md or design artifact: 不适用；本变更不涉及产品 UI 或交互设计。
- CONTEXT.md / ADR: 不适用；本变更不引入新的领域术语或架构决策。

## Why

当前 run router 级共享 `responses` 让 RUN-001 至 RUN-005 的运行时 OpenAPI 同时暴露生产路径不可能返回的 status，公开契约因此比 `API-Contract.md` 更宽。Phase 13.5 先收紧已实现接口的 OpenAPI 精确性，给后续 delegation 与 SSE 变更建立可信漂移门禁。

## What Changes

- RUN-001 至 RUN-005 按 operation 声明成功与错误 status，不再继承共享的多余 response metadata。
- 每个已声明错误 status 均引用 `ApiErrorEnvelope`，FastAPI 自动 validation response 也保持统一 envelope。
- contract tests 双向比较每个 operation 的 status 集合，既发现缺失 status，也拒绝额外 status。
- contract tests 固定当前 response schema：RUN-001、RUN-002、RUN-004、RUN-005 使用 `RunCreateResponse`，RUN-003 使用 `RunEventsResponse`。
- RUN-002 在本变更中不得切换到 `RunDetailResponse`；该切换仅能随 `agent-delegation-execution` 的 route、schema 与 drift test 原子落地。

## Non-Goals

- 不实现 RUN-006、SSE transport、`Last-Event-ID` 或流式响应。
- 不实现真实 delegation、parent/child run、usage/budget/trace 聚合或 `RunDetailResponse`。
- 不修改 RUN-001 至 RUN-005 的运行时业务语义、鉴权、policy、guardrail、持久化、queue 或错误码语义。
- 不实施或标记 Phase 14、Phase 15 完成。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `service-app-shell`: 把既有 P0 HTTP/OpenAPI 无漂移要求细化为 RUN-001 至 RUN-005 的 operation-specific status 与 response schema 精确契约。

## Impact

- API metadata: `templates/service-app/app/api/routes/runs.py`。
- 错误映射核验: `templates/service-app/app/main.py`。
- 契约证据: `tests/contracts/test_runtime_checkpoint_runs_contracts.py`。
- 不新增依赖、配置、数据库表、migration、UI 或发布行为；公开 endpoint 路径与运行时响应体不变，仅移除 OpenAPI 中不属于对应 operation 的多余 response 声明。
