## 来源链接

- Product-Spec.md：`SCOPE-001` 到 `SCOPE-003`，对应 uv workspace、包边界和 service-app template；`SCOPE-022`、`SCOPE-023` 和 `SCOPE-026`，对应 README、质量门禁和 Apache-2.0 合规。
- Product-Spec.md：`REQ-001` Monorepo / uv workspace 结构；`REQ-002` 核心包和上游隔离；`REQ-003` 后端 service-app template；`REQ-018` README 入口；`REQ-019` TDD 和质量门禁；`REQ-021` 许可证与合规。
- DEV-PLAN.md：`Phase 1: Monorepo 骨架与质量门禁地基`。
- Design-Brief.md 或设计稿：当前没有 Design-Brief。本次变更没有产品 UI 表面。
- CONTEXT.md / ADR：无。

## 为什么

在仓库具备稳定的 workspace 形态、包边界和最低验证主干之前，产品不能安全启动 runtime、storage、policy 或 eval 工作。本变更创建 Phase 1 基线，让后续 OpenSpec changes 能直接面向稳定路径和命令，而不是反复讨论项目结构。

## 变更内容

- 新增 `uv workspace` 根目录结构，区分 `packages/agent-harness`、`templates/service-app`、`examples`、`docs` 和 `scripts`。
- 新增可安装的 `agent-harness` 包骨架，可构建 wheel/sdist，并暴露带版本的公共包入口。
- 新增 `templates/service-app` shell，通过 workspace/path dependency 依赖 `agent-harness`，并预留 Product Spec 要求的 app、agents、config、eval、tests、docs 和环境文件布局。
- 新增最低开发命令，覆盖依赖同步、质量检查、测试、本地 smoke、包构建和许可证检查。
- 新增 README、LICENSE、NOTICE 和 pre-commit 入口，记录脚手架目的、目录边界和合规基线。

## 非目标

- 不实现 runtime orchestration、DBOS integration、storage repositories、migrations、event streaming、policy、HITL、tools、retrieval、observability adapters、eval runner 或 CI release automation。
- 不实现四个 example agents；除 service-app shell 所需的空 package marker 和目录结构外，不添加行为。
- 不添加产品 UI、SaaS management screens、用户注册、OAuth/OIDC 或 service-profile Docker orchestration。
- 不 vendor Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 或其他上游 SDK 源码。

## Capabilities

### New Capabilities

- `workspace-packaging`：定义 uv workspace、可安装核心包、构建边界和包依赖关系。
- `service-app-shell`：定义后端 service-app template shell、预留目录布局、本地 profile 入口，以及 app 入口代码与未来 agent 逻辑之间的边界。
- `quality-compliance-entrypoints`：定义功能开发开始前所需的最低质量、smoke、文档和许可证/合规入口。

### Modified Capabilities

- 无。当前还没有既有 OpenSpec baseline specs。

## 影响

- 受影响代码和文件：根目录 `pyproject.toml`、`uv.lock`、`Makefile`、`.pre-commit-config.yaml`、`README.md`、`LICENSE`、`NOTICE`、`packages/agent-harness/**`、`templates/service-app/**`、`examples/**`、`docs/**` 和 `scripts/**`。
- 受影响 APIs：不引入 runtime HTTP API；只建立包入口和开发者命令。
- 受影响依赖：uv、hatchling、ruff、pyright、pytest、pytest-asyncio、coverage.py、pre-commit 和 license-check 工具。FastAPI、Pydantic AI、DBOS、SQLAlchemy 和 provider SDKs 等运行时依赖不进入本变更，除非只是作为未实现的未来 optional dependency groups 声明。
- 受影响数据：没有数据库 schema 或 migrations。
- 受影响 UI 表面：无。
- 受影响系统：本地开发者工作流和未来 CI 命令契约。
