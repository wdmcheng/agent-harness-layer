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
service-app SHALL 使用统一 DTO 和 `ApiErrorEnvelope` 暴露 `API-Contract.md` 当前定义的 P0 endpoint；实现前 MUST 先为 health、单项 approval 读取和 `EVL-001` 至 `EVL-003` 的每个 operation 补齐字段级契约基线。所有 response MUST 携带适用的 `request_id`，run response MUST 携带 `run_id`，validation error MUST 使用 422 `ApiErrorEnvelope`。RUN-001 至 RUN-005 的运行时 OpenAPI response status 集合 MUST 与 `API-Contract.md` 逐 operation 精确相等，不得因 router 级 metadata 暴露生产路径不可能返回的 status；每个已声明错误 status MUST 引用 `ApiErrorEnvelope`。本变更最终将 RUN-002 原子切换为 `RunDetailResponse`；归档投影中该 response MUST 是唯一最终语义，不得继续保留 `RunCreateResponse` 的旧 MUST。

#### Scenario: OpenAPI 全量漂移检查通过
- **WHEN** contract test 将运行时 OpenAPI 与 `API-Contract.md` 当前 P0 path、method、schema、认证和错误响应对照
- **THEN** 所有已定义 endpoint 均有一致声明，且不存在缺失、额外或字段语义漂移

#### Scenario: 所有适用 operation 的 validation error 使用统一 envelope
- **WHEN** contract test 参数化遍历 API Contract 明确声明 422 的当前 P0 operations 并提交各自无效输入
- **THEN** 每个适用 operation 都返回 HTTP 422 和 `ApiErrorEnvelope`，其中含脱敏错误和 `request_id`，而不是 FastAPI 默认 detail body

#### Scenario: RUN-001 的 OpenAPI response 精确
- **WHEN** contract test 读取 `POST /api/v1/agents/{agent_id}/runs` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`202`、`400`、`401`、`403`、`404`、`409`、`422`、`500`、`503`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: RUN-002 的最终 detail schema 与 response 精确
- **WHEN** contract test 读取 `GET /api/v1/runs/{run_id}` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`500`，成功 response 只引用 `RunDetailResponse`，每个错误 response 引用 `ApiErrorEnvelope`，且 schema 不再引用 `RunCreateResponse`

#### Scenario: RUN-003 的 OpenAPI response 精确
- **WHEN** contract test 读取 `GET /api/v1/runs/{run_id}/events` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`422`、`500`，成功 response 引用 `RunEventsResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: RUN-004 的 OpenAPI response 精确
- **WHEN** contract test 读取 `POST /api/v1/runs/{run_id}/cancel` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`409`、`500`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: RUN-005 的 OpenAPI response 精确
- **WHEN** contract test 读取 `POST /api/v1/runs/{run_id}/resume` 的运行时 OpenAPI operation
- **THEN** response status 集合恰好为 `200`、`401`、`403`、`404`、`409`、`422`、`500`，成功 response 引用 `RunCreateResponse`，每个错误 response 引用 `ApiErrorEnvelope`

#### Scenario: Run response 漂移检查同时拒绝缺失和多余 status
- **WHEN** 任一 RUN-001 至 RUN-005 operation 的运行时 OpenAPI 相对精确基准缺失一个 status 或额外出现一个 status
- **THEN** contract test 必须失败，并定位发生漂移的 path、method、缺失集合与多余集合

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
service-app SHALL 在 service profile 分离 API/worker：RUN-001 先持久化 `status=created` 与私有 enqueue_pending，Redis 接受并记录 queued/message ref 后才发布 `run.queued`/返回成功；worker 消费同 message 执行。approve continuation 同样持久化可补投状态。deny 不排队、不调用 executor/tool，但 API 只原子提交 deny 仲裁与有序 outbox；公开 approval/run 必须保持 waiting，直到唯一 `approval.resolved` 与对应 failed/fallback terminal 依序持久化后才进入终态。local/CLI 继续 inline，并遵守相同的“resolution 先于 terminal”证据顺序。

#### Scenario: API 不在请求进程执行 agent
- **WHEN** service profile 调用 RUN-001 且 worker 暂停
- **THEN** API 返回同一 `status=created` run，run 不进入 terminal，executor 调用计数为零，message 留在 Redis 等待 worker

#### Scenario: Worker 启动后完成 API run
- **WHEN** 独立 worker 随后消费该 message
- **THEN** worker 执行 API 创建的同一 `run_id`，共享 PostgreSQL detail 与 event seam 最终返回 completed、failed 或 waiting 真实状态

#### Scenario: Local profile 无外部依赖回归
- **WHEN** 开发者使用 local profile 或 `agent-harness run`
- **THEN** run 继续通过 SQLite/local event seam inline 执行，不要求 Redis、DBOS system database 或 service worker；approval resolution 与 terminal 仍按相同顺序持久化

#### Scenario: Service approval API 不执行 approve continuation
- **WHEN** reviewer 在 service profile 批准 executor-produced waiting approval 且 worker 暂停
- **THEN** APR-002 完成 lease/policy/audit/enqueue 状态并返回 queued/in-progress 语义，executor/tool 调用计数保持零；worker 恢复后才执行原 continuation，并在 resolution/terminal evidence 均已持久化后公开 approved 与 run 终态

#### Scenario: Service deny 不进入 queue
- **WHEN** reviewer 在 service profile 拒绝同类 waiting approval
- **THEN** API 原子提交 deny 仲裁与有序 outbox，queue/DBOS operation 为零，worker 无需执行 continuation 且 handler 保持零；公开状态在 denied resolution 与 failed/fallback terminal 持久化前保持 waiting

### Requirement: Worker 只在确定性收口后确认 delivery
runtime worker MUST 在消费新消息前恢复同 tenant 的 run `enqueue_pending` operation；approve recovery 只允许 active `resolution_state=claimed`、`enqueue_pending` lease、尚无 tool claim 且已保存完整 reviewer/decision/规范化 request hash 的 operation，其他 approval state 封闭失败。approval pickup 必须先 CAS 为 `execution_owned` 并持久化 DBOS workflow owner/ref。pickup 到 API 中断窗口的 run message 先补齐 queued/message/`run.queued` evidence 再执行。run 到 waiting 后可确认 delivery；run 或 approval 进入公开终态前，worker MUST 确认该动作要求的 usage evidence、唯一 `approval.resolved` 与对应 terminal 已按顺序持久化。不确定异常或任一前置 evidence 未确认时不得 ack；确定性失败也必须先完成相同的证据顺序再 ack。恢复只补投稳定 ID 的 outbox，不得重放 provider、tool handler 或 continuation。

#### Scenario: 不确定失败保留 pending
- **WHEN** worker 在持久化执行结果、usage、approval resolution 或 terminal evidence 前遇到连接中断或被取消
- **THEN** delivery 未 ack，run/approval 不被伪造为公开终态，后续 worker 可 reclaim 同一 message，并只恢复未确认的执行步骤或稳定 outbox

#### Scenario: 确定性失败先落证据再 ack
- **WHEN** executor 返回受控失败或 approved tool 返回已持久化的确定性 failed result
- **THEN** worker 先确认适用的 usage、唯一 `approval.resolved` 与唯一 failed terminal 均已按序持久化，再 ack delivery；后续 reclaim 不再执行 provider、tool handler 或该 message

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

### Requirement: RUN-001 使用可选 caller trace 或服务端生成 trace
RUN-001 SHALL 接受可选 `X-Trace-Id`。合法 caller value 进入统一 runtime trace normalizer；缺失时服务端生成 canonical trace。空白、超长、非法字符或已绑定其他 root run 的 value MUST 在业务副作用前返回统一 `ApiErrorEnvelope`；公开 body 不回显内部 trace 生成细节。

#### Scenario: 缺失 header 仍建立 trace
- **WHEN** 已认证调用方不带 `X-Trace-Id` 创建 run
- **THEN** RUN-001 按当前 local/service success status 返回，后续 RUN-003 event evidence 可读取非空 canonical trace

#### Scenario: 非法 header 被拒绝
- **WHEN** 调用方提供空白、超长、非法字符或已绑定其他 root run 的 `X-Trace-Id`
- **THEN** API 返回 422 validation_error 或 409 trace conflict 的 `ApiErrorEnvelope`，且不创建 run、queue message 或 event

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

### Requirement: Runtime composition 统一注入 shared-budget seam
Local app、service API 与 worker composition SHALL 为 model、embedding、delegation 和 terminal guard 注入同一 shared-budget repository/UoW，并统一把 root 自身或 tenant-fenced delegation relation 解析为非空 `budget_owner_run_id`；P0 MUST NOT 新增公开 budget ledger HTTP route，也不得把内部 owner、余额、reservation、price secret 或 needs_review 细节加入公开 response。

#### Scenario: Local 与 service 使用同一合同
- **WHEN** 相同 parent budget 场景分别通过 local inline 与 service PostgreSQL/Redis 执行
- **THEN** 两条入口命中相同非空 owner 并得到逐值一致的 allow/reject、claim state 与公开错误语义，OpenAPI route 集合不增加 budget endpoint

#### Scenario: Direct budget reject 的公开 code 逐值一致
- **WHEN** local 或 service 的 direct model/embedding 因无可信有限上界、静态硬不合格、当前余额不足、snapshot无效或ledger needs-review而拒绝
- **THEN** 两条入口的module/runtime与usage rejection evidence都使用`budget.reservation_rejected`；内部reason可区分原因但不得进入公开response或泄露余额，delegation仍使用`delegation.budget_exceeded`

#### Scenario: Local 与 service 使用相同组合错误优先级
- **WHEN** local/SQLite 与 service/PostgreSQL 对相同 stable key、relation/snapshot、budget 与 event-capacity 组合执行新 claim 或 replay
- **THEN** 两条入口都按 exact replay/identity conflict、integrity、`event.sequence_state_invalid`、budget、`event.sequence_exhausted`、unique-race重读的顺序收敛；capacity-only逐值返回`event.sequence_exhausted`，budget+capacity返回对应budget code，数据库异常不进入公开消息

### Requirement: Service app 注册 RUN-006 精确契约
service app SHALL 注册 RUN-006，并在 OpenAPI 中精确声明 header/query、`text/event-stream` success content、JSON error envelope 与允许的 response status。route MUST 复用现有 run ownership、tenant、policy、event sink 和 request/trace correlation，不得创建第二套事件存储。

#### Scenario: OpenAPI 包含精确 RUN-006
- **WHEN** 生成 service app OpenAPI
- **THEN** RUN-006 path、parameters、success media type 与错误状态集合和 `API-Contract.md` 一致，不缺失也不暴露额外状态

#### Scenario: 不存在或跨租户 run 不建立 stream
- **WHEN** 调用方请求不存在或不属于当前租户的 run
- **THEN** API 在发送 SSE headers 前返回稳定 404 envelope，不泄漏其他租户事件
