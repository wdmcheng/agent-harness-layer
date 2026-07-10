## Source Links

- Product-Spec.md: `REQ-003` 后端服务型模板与 `AC-006`、`AC-007`；`REQ-008` API/CLI 管理面；`REQ-018` 中面向模板的 README 入口和目录边界切片。本 change 先证明 `make dev` 的 health/OpenAPI/serve 基础链与 copy-out wheel 安装；basic/fake 的最终 AC-006 显式 executor 运行和四个 P0 示例行为由后续示例 change 验收。
- DEV-PLAN.md: `Phase 12: Service App 模板与四个 P0 示例 Agent` 中的 Service App、P0 endpoint、OpenAPI 和 CLI 交付边界。
- API-Contract.md: 第 3-5 节通用 HTTP/DTO 契约、第 6-10 节 P0 endpoint、第 11 节完整 `HLT-001` Health API，以及第 14-15 节 OpenAPI 漂移和验收清单。
- Design-Brief.md or design artifact: 不适用；本 change 不新增视觉 UI，Swagger/Redoc 使用 FastAPI 默认管理面。
- CONTEXT.md / ADR: 当前仓库无相关文件，领域边界以上述产品、计划和 API 契约为准。

## Why

前序 Phase 已逐步提供 runtime、policy、approval、eval 等后端 seam，但 `templates/service-app` 仍缺少完整可复制的目录、health/CLI 入口、模板级测试与维护文档。Phase 12 需要先把这些分散能力收口成可运行、可验证且与 `API-Contract.md` 一致的 Service App 表面，四个示例 agent 才有稳定承载入口。

## What Changes

- 补齐 `templates/service-app` 的 `tests/`、`docs/`、eval case 目录、health route、模板 CLI 和开发/维护入口。
- 让 FastAPI app factory 注册 Phase 12 所需 P0 routes，并保留 Swagger/Redoc/OpenAPI 管理面。
- 为 local profile 提供无需真实 API key 的可运行入口，为 service profile 保留现有 PostgreSQL/Redis smoke 边界。
- 对运行时 OpenAPI 与 `API-Contract.md` 执行全量漂移检查，覆盖 path、method、DTO、认证、状态码、`request_id`、`run_id` 和 `ApiErrorEnvelope`（含 422）。
- 在实现/修改 route 前，把 health、单项 approval 读取和 `EVL-001` 至 `EVL-003` 扩展成第 3 节要求的字段级 endpoint 基线；`EVL-004` 仍属 Phase 12.5。
- 以模板级 contract/integration tests 固定入口编排、依赖注入和 vendor/ORM 边界。

## Non-Goals

- 不实现四个 P0 示例 agent 的业务流；它们由后续独立 change 交付。
- 不在本 change 重做核心 CLI 的 doctor、agents、run、approvals、eval、policy 或 scaffold 命令；本 change 只交付 app-specific `serve`，Phase 收口另行盘点既有 P0 CLI 并补齐缺口。
- 不独立收口完整 `REQ-018` 深度文档和 release 文档；本 change 只修正 Service App 模板 README 与模板维护入口，完整文档体系仍按 DEV-PLAN Phase 14 验收。
- 不实现 Phase 12.5 的 eval experiment/accept、behavior tags、optimization/holdout 或 harness hill-climb。
- 不实现 Phase 13 的 API/worker 物理拆分、远程 tool route、复杂 multi-agent graph UI 或生产部署。
- 不 archive 现有或本 change 的 OpenSpec artifacts。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `service-app-shell`: 将已预留的模板壳提升为具备完整目录、FastAPI health/API 管理面、模板 CLI、local profile 运行入口、测试和双受众文档的可复制应用模板。

## Impact

- 主要影响 `templates/service-app/app`、`templates/service-app/tests`、`templates/service-app/docs`、模板配置、Makefile、pyproject 和 README。
- 可能为复用既有公共 seam 对 `packages/agent-harness` 做窄幅适配，但核心包不得依赖模板。
- 不新增数据库表，不改变已归档主规格的既有 API 字段语义，不引入新的 vendor SDK。
