## 1. OpenSpec 与 API 契约

- [x] 1.1 运行 `openspec validate agent-registry-model-context --type change --strict`，修正 proposal/spec/design/tasks 直到可解析。
- [x] 1.2 扩展 `API-Contract.md` 的 `AGT-001` 为完整 endpoint 条目，写清 `/api/v1/agents` response schema、descriptor 可见字段、错误 envelope、重复 `agent_id` 与 registry validation error。
- [x] 1.3 新增 `/api/v1/agents` 局部 OpenAPI drift/contract tests，先证明 route/schema/error envelope 未实现或不匹配。

## 2. Agent Registry 与 API/CLI

- [x] 2.1 实现 `AgentDescriptor`、registry config schema、多 agent directory loader、重复 `agent_id` 和无效 config 错误；schema 必须覆盖 `agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model`、`budget`、`tool_allowlist`、`eval_dataset`、`delegation_edges`，public descriptor 必须使用相对 `config_ref`，并禁止暴露本地绝对路径、secret、callable、provider client 或其他 hidden 字段。
- [x] 2.2 实现 registry delegation check 与 delegation summary seam，覆盖未声明 edge 拒绝、已声明 edge 允许，以及 delegated usage/budget/trace refs 归并到 parent run 摘要。
- [x] 2.3 新增 `agent-harness agents list` CLI，使用 registry smoke config 并在无真实 API key 时可运行。
- [x] 2.4 新增 service-app `/api/v1/agents` route 和 app factory 依赖注入，错误统一映射为 `ApiErrorEnvelope`。

## 3. ModelRouter 与 Provider Boundary

- [x] 3.1 实现 `ModelProvider` interface、`FakeModelProvider`、Pydantic AI adapter 边界和 provider import boundary 检查。
- [x] 3.2 实现 `ModelRouter` 的默认/任务级模型选择、timeout、fallback、预算估算和 decision summary。
- [x] 3.3 为 ModelRouter/provider/budget 配置提供显式 reload 或 restart seam，并用 tests 锁住。

## 4. ContextAssembler 与 Embedding

- [x] 4.1 新增 Alembic revision、SQLAlchemy models 和 repository seam，创建 `context_assemblies` 与 `embedding_cache` 表，并覆盖 SQLite/PostgreSQL repository contract。
- [x] 4.2 实现 `ContextFragment`、token budget、truncation summary、fallback decision 和 `ContextAssembler.assemble()`，并写入可读取的 `context_assemblies` 记录。
- [x] 4.3 实现 `EmbeddingProvider`、mock/local provider、OpenAI-compatible adapter 边界和持久化 embedding cache。
- [x] 4.4 新增 contract tests 覆盖多来源 context assembly trace、预算降级、context assembly 持久化读取、embedding cache hit/miss 和跨 repository instance 命中。

## 5. 验证、归档与 Phase 收口

- [x] 5.1 运行 registry/model/context/embedding 局部 tests、`uv run agent-harness agents list`、`make smoke-local`，证明 fake model/mock embedding 离线可用。
- [x] 5.2 运行 `make quality`、`make test`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files`。
- [x] 5.3 派 code-reviewer 完成 Stage 1/2 review；如有实质改动，修复后重跑相关验证并重新 review。
- [x] 5.4 同步 main specs、归档 `agent-registry-model-context`，运行 `openspec validate --all --strict` 和 `openspec list --json`。
- [x] 5.5 更新 `DEV-PLAN.md` Phase 6 状态与剩余工作。
- [x] 5.6 clean review gate 通过后创建本地 commit。
