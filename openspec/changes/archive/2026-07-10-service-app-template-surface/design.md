## Context

前序 change 已在模板中逐步注册 runs、agents、policy、approval 和 eval routes，并提供 `RuntimeComponents`、local/service profile 与 worker shell；当前缺口集中在可复制目录、health/CLI 入口、模板级测试/文档和一次覆盖全部 P0 HTTP 表面的 OpenAPI 漂移证明。实现必须复用这些公共 seam，不能在 Phase 12 再造一套 runtime 或提前实施 Phase 13 的进程拆分。

## Goals / Non-Goals

**Goals:**

- 把已有模块组装成可启动、可发现、可测试的 Service App 模板。
- 用 health、CLI、OpenAPI 和 README 固定 app developer 与 scaffold maintainer 的公开入口。
- 用模板级 contract/integration tests 证明 P0 API、422 envelope、secret 脱敏和 import/ORM 边界。

**Non-Goals:**

- 不在本 change 实现四个 P0 示例 agent 的业务流。
- 不实现 eval experiment/accept、API/worker 物理拆分、远程 tool route、复杂多 agent workflow 或生产部署。

## Decisions

1. **继续以 `create_app` 为唯一 FastAPI application factory。** health router 与现有 route modules 一样在 factory 注册，测试通过依赖注入验证，不引入第二个 app 实例。替代方案是为模板另建 demo app；拒绝，因为会产生 OpenAPI 和异常处理双轨。
2. **health 只返回能力状态摘要。** response 从类型化 profile 读取 provider kind、enabled/degraded 等非敏感字段，禁止回显 DSN、token、endpoint credential 或绝对路径。替代方案是直接 dump settings；拒绝，因为会泄露部署细节和 secret reference。
   health 采用无需凭据的公开只读 liveness/capability 语义，不执行外部探测或写入；service readiness 的真实依赖证明仍由 `make smoke-service` 承担。
3. **模板 CLI 复用公共 app/runtime seam。** `serve` 负责启动 factory，其他能力优先委托核心 `agent-harness` CLI 或公共 service，不复制 registry/policy/storage 业务逻辑。替代方案是复制核心 CLI 命令；拒绝，因为行为和错误语义会漂移。
4. **OpenAPI 漂移采用 contract-first 顺序。** 先把 health、单项 approval GET、`EVL-001` 至 `EVL-003` 的每个 operation 补成第 3 节要求的完整字段表，再写会失败的参数化 drift/422 tests，最后实现 route；test 显式列出 Phase 12 paths/methods、通用 response 字段和 error models，同时保护 `/api/v1/tools` 不被提前公开。Phase 12.5 的 experiment/accept endpoints 不纳入本 change。
5. **模板缺失目录使用可提交占位说明保留。** `tests` 交付真实测试；空的 drafts/approved/docs 子目录通过 `.gitkeep` 或简短维护文档进入模板，不用运行时创建来掩盖源模板缺失。
6. **源仓库与复制项目使用两层依赖来源。** 模板只在标准 `project.dependencies` 声明版本化 `agent-harness`，不在成员 pyproject 固定 workspace source；仓库内由根 workspace source 覆盖，复制项目由 `make bootstrap AGENT_HARNESS_SOURCE=<wheel-or-source>` 写入可信本地 source，或由调用方显式允许已配置的可信 index。bootstrap 在复制项目无可信 source 时失败，避免误装同名公共包。替代方案是把核心包源码 vendoring 进模板；拒绝，因为破坏单向依赖和 wheel 安装验收。
7. **复制 smoke 必须真实离开 workspace，但不制造 executor 前置循环。** 本 change 的验证脚本使用当前已可构建核心 wheel、复制模板到临时目录、清除源码路径，bootstrap 后只证明 health/OpenAPI/serve 与 `.env` 提示；模板 Makefile 与 service smoke脚本只用模板自身路径，不依赖根 tests/scripts。basic/fake 显式 executor run由后续 P0 change在完成 executor seam后追加并重跑同一 copy smoke。

## Affected Surfaces

- `templates/service-app/app/main.py`、`app/api/routes/health.py`、`app/cli/main.py` 与包导出。
- `templates/service-app/tests`、`docs`、`eval-cases`、Makefile、pyproject、`.env.example`、README。
- `API-Contract.md` 的 health 完整条目和 Phase 12 全量漂移验收说明；不改变既有 endpoint 字段语义。
- `API-Contract.md` 的单项 approval GET、`EVL-001` 至 `EVL-003` 也补齐字段级 operation 基线；这属于文档契约收口，不扩大 runtime API。
- 无数据库 migration、无新外部依赖、无 UI 视觉设计。

## Testing Seams

- FastAPI `TestClient`/ASGI client：health、OpenAPI、Swagger/Redoc、422 envelope 和未定义 route。
- Typer `CliRunner` 或进程入口：模板 CLI help/serve 配置解析，不启动真实生产服务。
- local profile smoke：无真实 API key 创建 app/runtime，并验证公开入口。
- 静态 import boundary：模板 app/agents/eval runner 与核心包依赖方向。
- 文档/目录 contract：所需目录和双受众 README 内容。
- workspace 外复制 smoke：当前 wheel-only 安装、`.env` 提示、health/OpenAPI/serve 和无源码路径断言；P0 change 随后扩展为显式 executor basic run。

## Risks / Trade-offs

- [风险] `create_app` 已接近文件规模审查阈值，继续堆 handler 会降低维护性。→ health 放独立 route；异常注册若继续增长则提取到独立模块，但不做无关重构。
- [风险] 全量 OpenAPI 对照容易把 Phase 12.5 endpoint 误纳入当前范围。→ 以 Product-Spec/DEV-PLAN 的 Phase 边界筛选，明确排除 experiment/accept。
- [风险] health 回显 profile 配置导致 secret 泄漏。→ 使用专用 DTO 和 allowlist 字段，加入敏感值回归测试。
- [风险] 模板 CLI 与核心 CLI 重复实现。→ 模板 CLI 只提供 app-specific `serve` 和薄委托入口。
- [风险] 公共同名包被错误解析。→ 独立 bootstrap 默认要求本地 wheel/source；只有调用方显式配置可信 index 才允许版本化 index 解析。

## Migration Plan

本 change 仅补齐源模板和契约，无持久化迁移。回滚可恢复新增入口、bootstrap/smoke 脚本和模板文件；现有 runtime、storage 与前序 API route 保持不变。实现完成后保持 change 为 ready-to-archive，不自动同步或归档。

## Open Questions

- 无阻塞问题；uvicorn 启动参数沿用当前依赖版本和 FastAPI 默认 docs URLs，不新增 provider 或部署选择。
