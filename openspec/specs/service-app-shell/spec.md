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

### Requirement: Service profile 分离 API 提交与 worker 执行
service-app SHALL在 service profile分离 API/worker：RUN-001先持久化 `status=created`与私有 enqueue_pending，Redis接受并记录 queued/message ref后才发布 `run.queued`/返回成功；worker消费同 message执行。approve continuation同样持久化可补投状态；deny不排队。local/CLI继续 inline。

#### Scenario: API 不在请求进程执行 agent
- **WHEN** service profile 调用 RUN-001 且 worker 暂停
- **THEN** API 返回同一 `status=created` run，run 不进入 terminal，executor调用计数为零，message留在 Redis等待 worker

#### Scenario: Worker 启动后完成 API run
- **WHEN** 独立 worker 随后消费该 message
- **THEN** worker 执行 API 创建的同一 `run_id`，共享 PostgreSQL detail 与 event seam 最终返回 completed、failed 或 waiting 真实状态

#### Scenario: Local profile 无外部依赖回归
- **WHEN** 开发者使用 local profile 或 `agent-harness run`
- **THEN** run 继续通过 SQLite/local event seam inline 执行，不要求 Redis、DBOS system database 或 service worker

#### Scenario: Service approval API 不执行 approve continuation
- **WHEN** reviewer在 service profile批准 executor-produced waiting approval且 worker暂停
- **THEN** APR-002完成 lease/policy/audit/enqueue状态并返回 queued/in-progress语义，executor/tool调用计数保持零；worker恢复后才执行原 continuation

#### Scenario: Service deny 不进入 queue
- **WHEN** reviewer在 service profile拒绝同类 waiting approval
- **THEN** API原子收口 denied，queue/DBOS operation为零，worker无需参与且 handler保持零

### Requirement: Worker 只在确定性收口后确认 delivery
runtime worker MUST在消费新消息前恢复同 tenant的 run `enqueue_pending` operation；approve recovery只允许 active `resolution_state=claimed`、`enqueue_pending` lease、尚无 tool claim且已保存完整 reviewer/decision/规范化 request hash的 operation，其他 approval state fail closed。approval pickup必须先 CAS为 `execution_owned`并持久化 DBOS workflow owner/ref。pickup到 API中断窗口的 run message先补齐 queued/message/`run.queued` evidence再执行。run到 terminal/waiting后才 ack；不确定异常不 ack，确定性失败先写 failed terminal再 ack。

#### Scenario: 不确定失败保留 pending
- **WHEN** worker 在持久化执行结果前遇到连接中断或被取消
- **THEN** delivery 未 ack，run 不被伪造为 completed，后续 worker 可 reclaim 同一 message

#### Scenario: 确定性失败先落证据再 ack
- **WHEN** executor 返回受控失败且 runtime 成功持久化 failed terminal event
- **THEN** worker ack delivery，后续 reclaim 不再执行该 message

### Requirement: Service smoke 使用真实独立 API/worker
service-app的 `smoke-service` SHALL在仓库内和 workspace外复制项目中启动真实四服务，并分别证明：(1) initial DBOS owner/workflow已持久化后 hard crash -> Redis reclaim ->同 workflow恢复；(2) `examples.dev_assistant`产生 application waiting checkpoint，APR-002 approve经 worker恢复、deny零 continuation。脚本 MUST使用有效 service credential、共享 PostgreSQL/Redis，不得用 direct Python worker、DBOS metadata冒充 application checkpoint、共享 JSONL或日志推断替代。

#### Scenario: Workspace 外模板保留四服务证明
- **WHEN** smoke 把模板复制到 workspace 外，只安装已构建的核心 wheel 并运行 `make smoke-service`
- **THEN** 四服务使用复制项目自身的 Compose、profile 和脚本完成同一真实链路，不依赖仓库源码路径、根 `PYTHONPATH` 或 in-process fake

#### Scenario: Smoke 失败保留可操作诊断
- **WHEN** migration、Redis、API readiness、worker pickup、DBOS execution 或 event读取任一环节失败
- **THEN** smoke 以非零退出并指出失败边界与脱敏关联 id，不输出 DSN password、token、绝对敏感路径或 provider raw error

#### Scenario: Workspace 外 smoke 保留认证与资源隔离
- **WHEN** 复制项目运行四服务 smoke并在中途失败
- **THEN** service verifier仍拒绝缺失/无效凭据、有效临时凭据只在本轮生效，默认 cleanup删除复制项目本轮 containers/network/volume/queue/credential且不触碰仓库或其他项目资源
