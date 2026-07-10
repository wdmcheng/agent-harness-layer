## Purpose

定义 service-app template 的后端应用壳、目录边界、本地 profile 和文档入口要求。

## Requirements

### Requirement: Service-app template 暴露预留后端布局
service-app template SHALL 预留 Product Spec 要求的后端应用、agent、配置、eval、测试、文档和环境文件布局。

#### Scenario: Template 目录结构存在
- **WHEN** 开发者检查 `templates/service-app`
- **THEN** template 包含 `app/`、`agents/`、`configs/profiles/`、`eval-cases/drafts/`、`eval-cases/approved/`、`tests/`、`docs/`、`.env.example`、`Makefile`、`README.md` 和 `pyproject.toml`

### Requirement: App entry code 与 agent logic 分离
template SHALL 保持应用入口与未来业务 agent 实现目录分离。

#### Scenario: App entry 目录已预留
- **WHEN** 开发者检查 `templates/service-app/app`
- **THEN** 它包含预留的 `api/`、`cli/` 和 `workers/` 入口区域

#### Scenario: Agent 目录已预留
- **WHEN** 开发者检查 `templates/service-app/agents`
- **THEN** agent 实现预留在 agent 专属目录下，而不是放进 `app/*`

### Requirement: Local profile shell 无需外部 provider 凭据即可运行
service-app shell SHALL 提供本地开发 profile 和 smoke entrypoint，可在没有真实模型 key 或外部 observability providers 的情况下运行。

#### Scenario: Local profile 存在
- **WHEN** 开发者检查 `templates/service-app/configs/profiles`
- **THEN** `local.yaml` 存在，并声明适合后续 fake provider 和 local-jsonl integration 的本地默认值

#### Scenario: Local smoke entrypoint 存在
- **WHEN** 开发者运行 template 本地 smoke 命令
- **THEN** 命令只使用 workspace 本地配置即可完成，且不需要真实模型 key 或 SaaS provider 凭据

### Requirement: Template 文档标明 developer 和 maintainer entrypoints
service-app template SHALL 记录 agent 应用开发者如何从 template 启动，以及脚手架维护者如何让 template 与核心包边界保持一致。

#### Scenario: Template README 描述两个受众
- **WHEN** 开发者阅读 `templates/service-app/README.md`
- **THEN** README 标明 app 开发者设置步骤和脚手架维护者边界职责

### Requirement: Service-app template 提供可复制运行的完整应用表面
service-app template SHALL 提供 FastAPI、CLI、worker、local/service profile、tests、docs、eval case 目录、Docker Compose、环境示例、独立 bootstrap/service smoke 脚本和构建元数据，使开发者复制模板到 workspace 外后，能只安装本仓库构建的 `agent-harness` wheel 并从公开入口运行和验证应用。复制产物 MUST NOT 依赖仓库源码路径、根 `PYTHONPATH`、成员级 `workspace = true` 或固定 `cd ../..`。

#### Scenario: 完整模板目录和 profiles 可检查
- **WHEN** 开发者检查 `templates/service-app`
- **THEN** 目录包含 `app/api`、`app/cli`、`app/workers`、`agents/examples`、`configs/profiles/local.yaml`、`configs/profiles/service.yaml`、`eval-cases/drafts`、`eval-cases/approved`、`tests`、`docs`、`scripts/bootstrap.py`、`scripts/smoke_service.py`、`docker-compose.yml`、`.env.example`、`Makefile`、`README.md` 和 `pyproject.toml`

#### Scenario: Workspace 外复制并使用 wheel 启动
- **WHEN** contract smoke 把 `templates/service-app` 复制到 workspace 外，只把兼容的已构建 `agent-harness` wheel 作为核心包来源，清除仓库源码路径和根 `PYTHONPATH` 后执行 bootstrap 与 `make dev`
- **THEN** 复制项目依赖可解析，health 返回 200，OpenAPI/serve 可用；复制项目不解析同名未知公共包，也不要求真实 provider key。basic/fake agent 的执行必须遵循 registry/runtime 的显式 `AgentExecutor` 契约并由独立 run scenario 验证，不得用模板内固定 fake fallback 代替

#### Scenario: 缺少 env 文件给出可执行提示
- **WHEN** 复制项目首次 bootstrap 时存在 `.env.example` 但没有 `.env`
- **THEN** 命令输出复制 `.env.example` 为 `.env` 的明确提示；local profile 可继续使用安全默认值，service/secret override 不得被静默假定已配置

#### Scenario: Local profile 通过 make dev 启动
- **WHEN** 开发者在没有真实模型 key 或外部 SaaS provider 的环境使用 local profile
- **THEN** `make dev` 启动 FastAPI 开发入口，health、OpenAPI、Swagger/Redoc 可访问且不要求外部凭据；basic/fake 的最终 AC-006 运行证据与四个 P0 示例业务行为由后续 `p0-example-agent-flows` 在显式 executor seam 完成后验收

#### Scenario: Service profile smoke 连接真实依赖
- **WHEN** 开发者执行 `make smoke-service`
- **THEN** 模板自身携带的脚本在仓库内和 workspace 外复制项目中都能让 Docker Compose PostgreSQL/Redis service smoke 通过，并证明 latest migration、repository/UoW、worker 和 queue reachability 使用 service profile 而不是 SQLite/in-memory 伪证据

### Requirement: FastAPI app 暴露稳定管理面和 health 契约
service-app FastAPI app SHALL 注册 Product Spec 与 `API-Contract.md` 已定义的当前 P0 `/api/v1` routes，并保留 `/openapi.json`、Swagger 和 Redoc 管理面；health route MUST 是无需凭据的公开只读 liveness/capability endpoint，返回 profile、storage、queue 和 observability 的脱敏状态，不得建立未声明的远程 tool route。

#### Scenario: 管理面可发现
- **WHEN** 调用方创建模板 FastAPI app
- **THEN** `/openapi.json`、`/docs` 和 `/redoc` 可用，OpenAPI schema 包含已定义的 agents、runs、approvals、policies、eval 和 health paths

#### Scenario: Health response 不泄露连接凭据
- **WHEN** 调用方请求 `/api/v1/health`
- **THEN** response 包含 `request_id`、profile 和 storage/queue/observability 状态摘要，且不包含 DSN password、token、绝对本机路径或 provider secret

#### Scenario: 未定义 tool route 保持关闭
- **WHEN** 调用方读取运行时 OpenAPI schema
- **THEN** schema 不包含 `/api/v1/tools` 或其他未写入 `API-Contract.md` 的远程 tool execution route

### Requirement: P0 HTTP 契约与运行时 OpenAPI 无漂移
service-app SHALL 使用统一 DTO 和 `ApiErrorEnvelope` 暴露 `API-Contract.md` 当前定义的 P0 endpoint；实现前 MUST 先为 health、单项 approval 读取和 `EVL-001` 至 `EVL-003` 的每个 operation 补齐字段级契约基线。所有 response MUST 携带适用的 `request_id`，run response MUST 携带 `run_id`，validation error MUST 使用 422 `ApiErrorEnvelope`。

#### Scenario: OpenAPI 全量漂移检查通过
- **WHEN** contract test 将运行时 OpenAPI 与 `API-Contract.md` 当前 P0 path、method、schema、认证和错误响应对照
- **THEN** 所有已定义 endpoint 均有一致声明，且不存在缺失、额外或字段语义漂移

#### Scenario: 所有适用 operation 的 validation error 使用统一 envelope
- **WHEN** contract test 参数化遍历具有 request body、path 或 query validation 的当前 P0 operations 并提交各自无效输入
- **THEN** 每个适用 operation 都返回 HTTP 422 和 `ApiErrorEnvelope`，其中含脱敏错误和 `request_id`，而不是 FastAPI 默认 detail body

### Requirement: Template 入口保持编排与 vendor 边界
service-app 的 API、CLI、worker 和测试入口 MUST 只通过 `agent_harness` 公共 seam 编排能力；业务逻辑 MUST 留在 agent 目录，模板 app 和 eval runner MUST NOT 直接 import vendor SDK 或操作 ORM session。

#### Scenario: 静态边界扫描通过
- **WHEN** boundary test 扫描模板 app、agent 和 eval runner
- **THEN** 不存在直接 `pydantic_ai`、`dbos`、`logfire`、`phoenix`、`langfuse` import，不存在 SQLAlchemy session 直连，且核心包不依赖模板模块

#### Scenario: README 服务两个受众
- **WHEN** app developer 或 scaffold maintainer 阅读模板 README
- **THEN** 文档分别给出启动、配置、API/CLI 使用方式，以及核心包、模板入口、业务 agent 和 adapter 的维护边界

### Requirement: 模板 CLI 保持薄入口并复用核心命令
service-app 模板 CLI SHALL 只实现 app-specific `serve`。`doctor`、agents、run、approvals、eval、policy 和 scaffold MUST 由核心 `agent-harness` CLI 提供唯一业务实现；模板可以记录或调用这些核心入口，但 MUST NOT 复制命令逻辑、另建同义 command group 或形成第二套错误/输出语义。

#### Scenario: 模板 CLI 保持薄入口
- **WHEN** 开发者执行模板 CLI `--help`
- **THEN** 可发现 app-specific `serve`，且 agents/run/approvals/eval/policy 等能力仍指向核心 `agent-harness` CLI，不存在第二套实现

#### Scenario: 核心 CLI 能力不被模板复制
- **WHEN** 开发者从 service-app 项目执行 doctor、agents、run、approvals、eval、policy 或 scaffold 命令
- **THEN** 调用使用核心 `agent-harness` CLI 的同一 command、DTO、error 和 service seam；模板不维护独立实现
