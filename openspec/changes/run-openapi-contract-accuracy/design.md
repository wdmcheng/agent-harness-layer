## Context

`templates/service-app/app/api/routes/runs.py` 当前在 run router 上配置一份共享错误 response map。FastAPI 会把该 map 合并到 router 内每个 operation，导致 RUN-002、RUN-003、RUN-004 和 RUN-005 的 OpenAPI 同时出现并非各自生产路径合同的 `400`、`409`、`422` 或 `503`。实际 handler 已通过统一异常映射产生 `ApiErrorEnvelope`，本变更只修正公开 metadata 与精确漂移证据，不改变运行时业务语义。

上游边界要求 RUN-002 目前仍返回 `RunCreateResponse`。`RunDetailResponse` 依赖真实 child run 与 durable delegation aggregation，只能在后续 `agent-delegation-execution` 中与实现和测试一起切换。

## Goals / Non-Goals

**Goals:**

- 让 RUN-001 至 RUN-005 的 OpenAPI status 和 schema 与 `API-Contract.md` 逐 operation 精确一致。
- 保持所有公开错误 response 的 schema 为 `ApiErrorEnvelope`。
- 建立双向漂移测试，使缺失和多余 status 都会失败。
- 固定 RUN-002 当前 `RunCreateResponse` 边界，防止未来 schema 被提前声明。

**Non-Goals:**

- 不修改 runtime、queue、storage、auth、policy、guardrail 或 event 行为。
- 不实现 RUN-006、SSE 或 `Last-Event-ID`。
- 不实现 delegation、parent aggregation 或 `RunDetailResponse`。
- 不涉及 Phase 14、Phase 15、发布或 archive。

## Decisions

### 1. 由 operation 自己声明 response metadata，并受控移除不适用的自动 422

移除 run router 级共享 `responses`，在五个 route decorator 上分别声明该 operation 的错误 response，并保留 RUN-001 的显式 `202 RunCreateResponse`。FastAPI 会为带 path/query/body 参数的 operation 自动生成 422；对 API Contract 没有 validation error 的 RUN-002/RUN-004，应用的唯一 OpenAPI factory SHALL 按精确 allowlist 移除自动 422。具有真实 validation 路径的 RUN-001/RUN-003/RUN-005 保留 422，并继续由统一 handler 返回 `ApiErrorEnvelope`。

备选方案是把 FastAPI 自动 422 一律写进 API Contract。该方案会公开生产路径无法触发的状态，并削弱精确契约，因此不采用。受控 OpenAPI factory 只能移除明确 allowlist 中的不适用自动 422，不能泛化修改其他 status；最终精确集合测试防止框架升级后静默漂移。

### 2. 用精确集合而不是子集断言

contract test 以 `(path, method)` 为键保存 expected success/error status 和 success schema，直接比较集合相等，并对每个错误 response 检查 `ApiErrorEnvelope` 引用。失败信息同时报告 missing 与 extra，避免只证明“应该有的存在”却放过契约扩张。

备选方案是分别写零散的 `in responses` 断言。它无法拒绝额外 status，也不能形成统一对账表，因此不采用。

### 3. RUN-002 保持当前公共 DTO

RUN-002 的 success schema 在本变更固定为 `RunCreateResponse`，测试还要拒绝该 operation 引用 `RunDetailResponse`。后续 delegation change 必须原子更新 route、schema、API contract delta 与同一漂移表，不能只增加一个未被生产数据支撑的 OpenAPI model。

备选方案是现在先声明 `RunDetailResponse`，字段暂时填空。该方案会把未来能力误写成当前能力，并让调用方依赖没有 durable 证据的字段，因此不采用。

## Affected Surfaces

- `templates/service-app/app/api/routes/runs.py`: router metadata 与五个 operation 的 response map。
- `templates/service-app/app/main.py`: 唯一 OpenAPI factory 对 RUN-002/RUN-004 的框架自动 422 做窄化 allowlist 过滤。
- `templates/service-app/app/api/errors.py`、`templates/service-app/app/main.py`: 只核验既有异常到 status/body 的映射，不计划改变错误语义。
- `tests/contracts/test_runtime_checkpoint_runs_contracts.py`: RUN-001 至 RUN-005 精确 status/schema 漂移表。
- API path、request body、运行时 response body、数据库、migration、依赖、配置、CLI、UI 和部署均不变。

## Testing Seams

- 通过 `create_app().openapi()` 读取实际运行时 schema，不读取 decorator 私有常量作为替代证据。
- 对五个 `(path, method)` 逐项比较完整 response status 集合。
- 对 `200/202` success response 检查对应 DTO `$ref`；对所有错误 status 检查 `ApiErrorEnvelope` `$ref`。
- 保留现有真实无效输入测试，证明适用 operation 的 HTTP 422 body 仍由统一 handler 映射，而不只是 OpenAPI metadata 正确。
- 最小 FastAPI 回归夹具证明框架确实自动生成 422，并证明过滤只影响 API Contract 明确排除 422 的两个 operation。
- 运行既有 run runtime、auth/policy、split-runtime 和 local/service smoke，确认 metadata 收紧没有改变生产行为。

## Risks / Trade-offs

- [FastAPI 或 router 组合方式变化后重新引入默认 response] → 精确集合测试直接读取最终 `app.openapi()`，任何多余项都会失败。
- [声明的 status 与真实异常映射不一致] → 以 `API-Contract.md` 为 status 基准，并保留真实错误路径 contract tests；不能只验证 schema 文档。
- [后续 RUN-002 切换遗漏 drift table] → 当前测试固定 success schema，delegation change 必须在同一 tracked diff 中更新实现与期望。
- [多个 decorator 重复错误 map 增加维护成本] → 允许使用只返回指定 status 的小型 metadata helper，但每个 operation 的最终集合必须在 route 附近可审查，且测试基准不得从生产 helper 派生。

## Migration Plan

本变更不修改 endpoint 或响应体，因此无需数据迁移和调用方迁移。实现后先运行定向 contract tests，再运行完整 quality/test/local smoke/service smoke；通过后停在 `ready-to-archive`。回滚只需恢复 router/operation response metadata 和对应测试，不涉及数据库回滚。

## Open Questions

无。RUN-002 切换 `RunDetailResponse`、RUN-006 SSE transport 均已有后续 change 边界，不在本变更中决策。
