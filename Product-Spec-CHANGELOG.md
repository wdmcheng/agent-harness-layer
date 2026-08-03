# 变更记录

## [v1.24] - 2026-08-02
### Provider-neutral Structured Output 契约

- 新增 P1 `SCOPE-034`、`TASK-016`、`REQ-028` 与 AC-096 至 AC-103，把 Phase 19 从计划意图收敛为稳定 schema identity、provider-neutral structured result、有限 repair、共享预算、耐久 evidence 与 exact replay 的可验收行为。
- Agent Registry 是输出 schema 的授权真相源；descriptor 只公开 `schema_ref/version/digest`，provider-native/Pydantic AI 类型不得进入核心 DTO、公开 API 或持久化证据。结构化成功仍保留与 canonical JSON 一致的 `ModelResponse.output_text`。
- 冻结adapter到核心validator的唯一`StructuredProviderCandidate` exact DTO与prepared protocol签名；核心显式拥有repair×transport双层循环，每对ordinal使用fresh prepared call，send只做一次外部request且禁止隐藏retry；candidate删除重复顶层计量，sole local attempt是唯一usage/cost/latency来源，prepare/call错误使用核心公开的vendor-neutral异常，原始值不耐久、不进入repair prompt或日志，SDK/裸对象/`ModelResponse`旁路关闭失败。
- 明确额外字段、unknown schema、capability unsupported、预算不足、repair exhausted、replay conflict 和 unknown/needs-review 的 fail-closed 语义；Phase 19 不做 structured streaming、跨 provider structured fallback、tool call/执行、fake 隐式后备或 Phase 21 重构。
- 冻结 structured route 边界：Agent 只要显式声明任意非空 `fallback_routes` 就保持 Phase 18.2 route-chain identity，即使请求缩权后只有一个候选也在副作用前拒绝，不把显式 chain 降级成 legacy 单 route。
- Structured preflight唯一映射新增`model.structured_policy_invalid`；deployment capability或provider protocol缺失都统一为`model.structured_capability_unsupported`，底层`model.capability_unsupported`不得逸出公开structured seam。
- 结构化not-started proof只由核心在prepared send前构造；只有显式retryable prepare错误能推进下一transport ordinal，一旦到达send或收到HTTP response就计为provider request并停止structured retry，endpoint classifier继续只服务既有text路径。Send前/后取消、deadline与prepared close失败的failed/needs-review优先级已逐边界冻结。
- 全量Registry严格加载的兼容迁移同时覆盖Dev Assistant宽松工具结果和RAG Assistant宽松组裁字典；RAG收紧为封闭联合，`no_source`只接受精确`{}`且不伪造零计数或assembly记录，`completed`只接受映射Context Assembly既有六个计数的严格DTO；不放宽strict compiler、不改写耐久组裁schema或示例业务语义。
- 冻结 strict schema compiler 的位置感知关键字集合并拒绝 `format`/条件/unevaluated 等本阶段未支持语义；validation issue只消费validator顶层迭代结果、不遍历组合器context，避免同一值产生不同repair prompt和replay evidence。
- 本条只记录需求与验收基线；生产实现、测试、fresh review 和 ready-to-archive 状态必须分别取得证据，不能由本文或 OpenSpec PASS 冒充。
- Phase 19最终契约为14 Requirements/74 Scenarios，身份`7754ef26…`与实现身份`de39eb09…`均分别由fresh Reviewer 1/2/3完成Stage 1/2 PASS、0 findings，44/44 tasks；AC-096至AC-103已按生产、测试、eval与验收矩阵证据闭合。OpenSpec于2026-08-04授权事务中把12条新增、2条修改同步到六份主规格，并归档至`openspec/changes/archive/2026-08-03-provider-neutral-structured-output/`；归档日期采用CLI实际生成值。acceptance旧CI evidence与live外部前置仍分别保持`BLOCKED`和零调用`hosted-unverified`，归档不代表commit、push、发布、部署或真实provider验证。

## [v1.23] - 2026-07-31
### 受控多 deployment fallback 实现

- 接入有序 route refs、冻结 chain identity/state、逐 attempt started/proof lifecycle、shared-budget v2 claim、审批前稳定身份与原 claim激活、completion/streaming 首 delta 围栏和 `0017_model_route_chain_state` 迁移。
- 恢复会从冻结chain逐项重算历史attempt/proof摘要并拒绝形状合法但内容被同步篡改的状态；send前已耐久started identity在安全错误摘要中计入attempt数量，但不会被误报为provider已调用。
- 真实adapter在connect/client边界明确未发送时，completion与streaming统一生成`client_not_started` canonical proof并只调用次选一次；adapter内部“未观察到完成”的false判别不会作为响应事实写入proof。
- Lazy client/transport或agent构造在send/iterate前确定失败时，completion与streaming统一关闭当前attempt并安全推进，不再把零client/零request误记为unknown/provider已调用或泄漏内部构造异常；显式取消与deadline仍保持不fallback。
- 显式取消若发生在attempt started identity已耐久之后，绝不授权retry或fallback；`prepared`只证明本地资源所有权，不能据此推断request已发送或provider已调用。只有provider-neutral stream关闭结果证明`stopped + complete usage`且无durable delta不确定性时，当前attempt才以actual usage收敛为`cancelled/invocation_cancelled`，selected为空且不发布completed；其余completion/streaming取消都关闭为unknown、保留reservation/capacity并提升needs-review，稳定错误按明确request/response/result/usage/text/delta事实如实报告`provider_called`。
- 新增四分支 `model-failover-live-smoke/v1` producer/validator 及 CI evidence；默认前置不足时零调用并报告 `hosted-unverified`，只有双 deployment、隔离 credential/endpoint 和受控 not-started fixture全部满足后才允许真实验证。
- 该版本记录本地实现事实，不代表真实 provider PASS、最终审查、归档、发布或部署已经完成。

## [v1.22] - 2026-07-29
### 受控跨 deployment/provider fallback 契约

- 新增 P1 `SCOPE-033`、`TASK-015`、`FLOW-008`、`REQ-027` 与 AC-090 至 AC-095，把此前只有后置意图的多 provider fallback 固定为独立 `controlled-multi-provider-failover` 增量契约。
- Fallback候选不再只表达 model ID，而以有序 `(deployment_id, model_id)` route chain表达；chain mode既有单值 model字段只作为请求缩权前原始 Agent首 route兼容投影，不授权后继，也不随 request删除首 route而改写。Provider kind、endpoint、credential、catalog、能力、重试、Bulkhead和价格均从每个候选自己的 typed deployment派生，request只能删减，不能新增或重排。
- 跨 provider 切换只允许发生在当前候选每个实际 attempt 各自以 `client_not_started`，或 endpoint-bound classifier、deployment显式状态白名单与零生成/计量事实共同证明的 `trusted_business_not_started`耐久收敛后；同候选内两类 proof可混合。Legacy单route保持既有`reservation → permit → client → durable side_effect_started → send`；显式chain冻结为`candidate reservation → durable attempt started identity → permit → isolated client/prepare → send/iterate`。每次首次调用或同 route retry都先创建独立耐久attempt identity，proof/unknown/settlement再原子关闭；任何started悬空或确认未知都不自动重发。403默认不启用，同 route retry先于跨 provider；无受信证明的 timeout/response、unknown、response identity、usage、文本或 delta都立即停止。Streaming观察或提交首个 delta后永久禁止切换 provider。
- 每个候选独立预约和结算；静态不合格与 soft-threshold/current-balance预算不可用分别成为零调用的 `static_ineligible`和 `budget_ineligible`耐久状态。当前 reservation在一个 owner lock/CAS事务中跨过中间不可用候选直达首个 eligible后继，或在无后继时原子 actual-zero结算、释放并 exhausted；恢复不按后来余额重选。调用级 claim的 started是整链单调高水位，逐候选聚合 side-effect/request/response与逐 attempt proof records分层记录。即使前序 candidate或同候选早期 retry已使高水位 started，后续 attempt仍可凭自身 `client_not_started`安全收敛，但高水位不得回退、已耐久记录的 provider attempt不得重放。Route chain在 approval前冻结 usage call id与 operation identity；审批等待、ApprovalService record/grant和 shared-budget activation复用原 claim，通过 digest和 fencing两阶段交接，禁止 approval id rekey或第二 claim；获批候选余额不足时后继重新授权，unknown或 proof缺失/冲突保留原 reservation并进入 needs-review。
- 既有单 route/embedding 的预算身份逐字保留 `budget-operation-v1`；显式 route chain 使用 `budget-operation-v2`，以 ordinal 1 兼容投影和完整 chain digest/count 固定 replay identity，不因 current balance 或 active candidate 改写。
- 默认 fake/local 仍保持零网络，真实链耗尽不得隐式切 fake；双真实 deployment/provider 验证缺少任一授权、隔离凭据、受信 endpoint 或受控 not-started fixture 时保持零调用 `hosted-unverified`。只有两条单 attempt route、`[1,2]` ordinal、首项可信 proof、次项唯一 completed 与 route-chain/usage/cost durable evidence逐值一致时才能记 PASS。
- 澄清 AC-087 的流式读取可见性：默认公开 reader 只返回 public delta、completion 与 run terminal；获得内部读取权限后才额外可见 started/usage。两种模式都只读取已提交事件，断线或重连不会触碰 provider。

## [v1.21] - 2026-07-29
### 受控真实非流式文本模型运行时

- 交付 `REQ-025` 的受控真实非流式文本模型纵向闭环：真实 deployment 只能由 typed config 显式启用，credential reference、安全 endpoint policy、model catalog、冻结 route、策略/审批/审计、共享预算和 provider-neutral evidence 共同约束 provider 副作用。
- 配置覆盖 deployment/provider/model allowlist、default/fallback、`base_url`、credential ref、deadline、有限 retry/backoff、Bulkhead、价格目录和 capability；direct env 与 `_FILE` 冲突、secret 泄漏、未受信 origin、credential forwarding 越界及 encoded path 绕过均在 client、DNS、HTTP 前 fail closed。
- 路由固定为 deployment、Agent policy 与 request 的只缩权交集；请求不得选择 endpoint、credential 或 SDK 对象。未显式选模时，只能按冻结的同 deployment/provider 候选顺序执行 model fallback，并用各候选 catalog、价格、能力和 hard limit 重新判断；非法或空 route 不得静默回退 fake。
- Pydantic AI/OpenAI 类型仅存在于 vendor adapter 边界；真实调用使用可取消 async I/O、total deadline、有界 retry、`Retry-After`、可信 completion classifier 和 Bulkhead。started/unknown 失败禁止盲目重放，每个 attempt 的 route、usage、cost、latency 与副作用状态进入去敏 evidence。
- 预算预约、Policy/HITL、授权审计、provider 调用和 settlement 按冻结顺序执行；稳定失败、response、attempt 与 budget charge 在发布 final event/telemetry 前统一校验，SQLite/PostgreSQL 恢复保持相同错误、usage/cost 和调用事实，不重复 provider 副作用。
- local/service profile、scaffold 与模板 Agent 继续显式使用 `fake_default`；默认 unit/contract/eval/smoke-local 不读取 provider ambient env、不需要真实凭据且保持零网络，真实失败不会静默变成 fake 成功。
- AC-077、AC-078、AC-079、AC-080、AC-082、AC-084 已由离线 contract/integration/service 证据闭合；AC-081、AC-083 因两次授权 MiMo 入口均未建立真实 completion PASS 而保持未完成，其中第二次按 side-effect unknown 保留。Streaming、structured output、tool loop 与跨 deployment/provider fallback 继续由后续独立 Phase 交付。
- `controlled-real-model-runtime` 已同步主规格并归档，由本地提交 `ff0c49b` 交付；该生命周期状态不代表真实 provider 成功、发布、部署或生产流量切换。

## [v1.20] - 2026-07-27
### 架构演进治理、受控真实模型配置与增量文本流计划

- 新增 SCOPE-030 至 SCOPE-032、TASK-012 至 TASK-014、FLOW-006/007、REQ-024 至 REQ-026 与 AC-073 至 AC-088，把架构原则、人与 Agent 共用代码规范、living plan、change matrix、受控真实文本模型入口和 provider-neutral 增量文本流纳入产品事实源。
- 明确本轮不是大爆炸重构，也不按设计模式数量验收；后续以变化轴、不变量、窄 OpenSpec change、red contract、文件所有权和可执行架构门禁逐步演进。
- 修订 REQ-004 的配置边界：公开合并顺序为 profile YAML → Agent YAML → `.env` → secret file → direct 进程环境 → 受控 overrides；`.env` 高于 YAML 但不是 secret manager，且只解析 `AGENT_HARNESS_*`，provider 原生 ambient env 不能成为第二条不可见配置路径。
- 扩充真实模型 deployment 配置契约：区分 deployment id 与 provider kind，覆盖模型 allowlist/default/fallback、`base_url`/endpoint policy、credential reference、连接/读取/总 deadline、retry/backoff、并发舱壁、价格目录版本和 capability flags。
- 固定真实模型安全路由：部署允许列表、Agent 冻结策略和请求意图求交集，先生成 immutable route plan，再进入预算预约、授权成功审计与 provider 副作用；拒绝路径仍可写去敏本地审计。`base_url` 虽通常非秘密，但必须与 credential origin 绑定并拒绝未批准 scheme/origin。
- 首个实现 change 仍为 `controlled-real-model-runtime`，只做非流式真实文本 completion，以先冻结 deployment、route、secret、endpoint、预算和取消基线；其完成并归档后立即进入独立 P0 Phase 18.1 `controlled-model-streaming`，不再把 streaming 留成无顺序的笼统后置项。Structured output、模型工具循环与多 provider 运维继续后置。
- Phase 18.1 复用既有 CanonicalEvent / RUN-006 / CLI reader，不新增第二个流状态源；固定有界 delta/coalescing 与 event-capacity reservation、completed/usage/terminal 顺序、跨 chunk 输出安全、subscriber 断线不隐式取消、显式取消后的 partial usage/unknown、禁止 provider 重放，以及 transport 首 frame 与 provider 首 delta 的指标分离。当前 adapter/composition 仍不具备这些能力，本次只落盘需求与计划，不宣称实现完成。
- 修复 live 验收 identity 冲突：保留 v1.17 dependency lock 的 `AC-070`，把 v1.19 后加入的 API docs 关闭行为从旧 `AC-070` 迁移为 `AC-089`；历史 changelog 与 OpenSpec archive 保持原样，由本条提供行为限定的迁移追溯。

## [v1.19] - 2026-07-25
### API 文档生产关闭边界

- 新增 AC-070：Swagger UI、Redoc、OpenAPI schema、OAuth2 redirect 与文档静态 mount 必须由一个类型化开关整体控制，关闭时不读取静态资源。
- `local` profile 默认开启文档，面向正式部署的 `service` profile 默认关闭；两者都允许通过 `AGENT_HARNESS_SERVICE__API_DOCS__ENABLED` 显式覆盖。
- 本轮继续作为已归档 Phase 12 的窄范围模板维护，不新建 OpenSpec change。

## [v1.18] - 2026-07-25
### 复制模板的离线 API 文档与评测自准备

- 收紧 REQ-003 / AC-006：复制后的 service-app 必须默认自托管锁定版本的 Swagger UI / Redoc 静态资源，在无外网环境仍可使用；仅允许通过类型化配置显式切换到同版本 CDN。
- 模板必须带可复现的资源更新入口，记录版本、来源、SHA-256 和许可证文件，下载或校验失败不得留下部分更新。
- 收紧 AC-047：每个 `make eval*` 都必须先迁移自己的独立 SQLite，全新 `STATE_DIR` 可直接运行 fake-model 评测；保持 runtime 的未迁移 schema fail-closed 边界。
- 本轮是已归档 Phase 12 的窄范围模板维护修复，当前无 active OpenSpec change，不创建新 change，不修改主 OpenSpec specs 或 archive。

## [v1.17] - 2026-07-23
### 依赖兼容范围与精确锁定分层

- 新增 REQ-023 与 AC-069/070/071/072：三份 `pyproject.toml` 的外部 runtime、optional、dev、license、release 和 build-system 依赖以已验证下界加兼容上界声明；根与模板对同仓库 `agent-harness` 保持项目版本精确匹配，`uv.lock` 继续保存精确解析，外部依赖升级必须显式发起。
- 根 `[tool.uv].required-version` 从单一 `0.11.29` 调整为已验证的 `>=0.11.19,<0.12`；GitHub、GitLab、release wrapper、容器 digest与发布证据仍精确固定 `0.11.29`，避免把本地 patch 兼容性和发布可复现性混为一谈。
- release promotion 必须把根 workspace 与模板的 `agent-harness` 自依赖同步为当前完整版本的 exact pin，禁止发版后产生范围或通配形式；外部声明放宽不得改变 `uv.lock` 的 `(name, version, source)` 身份。
- build-system 对消费者暴露兼容范围，但仓库 preview 与正式 tag build 必须先 frozen sync，再关闭默认 build isolation 使用 lock 内精确 Hatchling，并在 manifest 中记录、核对 backend identity；不能把默认隔离构建误写成受项目 lock 约束。
- `relax-dependency-version-constraints` 的 19/19 tasks 已完成；delta 已同步到依赖版本策略、维护文档、发布构建和 workspace 包边界四类长期主规格，并归档到 `openspec/changes/archive/2026-07-23-relax-dependency-version-constraints/`。归档不代表 commit、push、tag、release、真实 publish、依赖升级或部署。
- 归档后 fresh review 发现依赖策略合同仍读取已移动的 active `tasks.md`；精确节点先复现 `FileNotFoundError`，再切换为读取长期 `dependency-version-policy` 主规格。固定 uv `0.11.29` 下归档后 unit-contract 为 `1279 passed, 200 skipped`，全量 pytest 为 `1291 passed, 223 skipped`；系统 uv `0.11.19` 触发 release exact 基线拒绝的运行不计为候选证据。

## [v1.16] - 2026-07-23
### 深度维护文档引用链双语化

- 将英文 README、英文五层两翼指南和英文模板入口直接引用的维护文档统一为“原路径英文主文件 + `.zh-CN.md` 中文版”，避免英文导航落入中文正文。
- 双语范围覆盖 architecture、extension、adapter、context/trust、eval/observability、security、release、3 份 ADR，以及模板内文档地图和示例 Agent 指南；两版互链并保持命令、合同、当前/未来边界和证据状态一致。
- 收紧 AC-049：scaffold maintainer 从任一语言入口都必须能沿同语言引用链找到完整维护资料，不改变 runtime、API、CLI、配置或发布行为。

## [v1.15] - 2026-07-23
### 双语实操文档与 AI / Agent 协作入口

- 将新增实操文档纳入中英文配对规则：五层两翼开发指南维护英文主文件 `docs/building-an-agent.md` 与中文文件 `docs/building-an-agent.zh-CN.md`，两份内容互链并保持事实一致。
- 要求 `templates/service-app` 随模板提供普通双语指南 `docs/ai-agent-guide.md` 与 `docs/ai-agent-guide.zh-CN.md`，通过 README 链接或用户明确提示交给 AI / Agent；不占用 `AGENTS.md` 这类会自动施加目录级规则的特殊文件。
- 明确源码仓库与复制模板的上下文差异，避免 AI 依赖复制后不存在的根级真相源；同时增加可复制的初始化项目与实现功能任务模板。
- 文档改动只要求最小充分的文档与契约验证，不把无关全量测试写成默认动作；提交、push、部署及真实外部副作用仍需用户单独授权。

## [v1.14] - 2026-07-23
### README 使用与维护契约细化

- 新增根目录与 `templates/service-app` 的中英文 README 配对要求，统一使用 `README.md` 作为英文入口、`README.zh-CN.md` 作为中文入口，并要求同目录互链和事实一致。
- 扩充 README 的可操作内容：环境准备、第一次使用、日常指南、CLI/HTTP/Python API、便捷封装的实际用法、模块设计、开发测试、贡献、安全与排障；字段级契约继续由 `API-Contract.md` 和深度文档承载。
- 新增五层两翼到实际 Agent 开发动作的映射与完整指南，明确接入/运行时由模板复用、引擎是业务主体、工具/基础设施按需扩展，以及 Eval/Observability 如何贯穿生命周期；同时标明架构图中的未来扩展位和概念性工具标签。
- 收紧 AC-048：新开发者使用任一语言都必须能从零完成上手，并理解目录职责、便捷封装与禁止跨边界规则；不改变 runtime、HTTP schema、CLI 或部署行为。

## [v1.13] - 2026-07-21
### Phase 15 实现与审查边界同步

- 最终联合审查确认的 `2 HIGH + 1 MEDIUM + 1 LOW` 已按最小范围修复，fresh Reviewer 1 对四项修复给出 Stage 1/2 PASS。当前候选证据包含 quality/ruff/pyright/import-boundary PASS、unit-contract `1266 passed, 200 skipped`、test-aggregate `1278 passed, 223 skipped`、integration `11 passed, 23 skipped`、eval 与 `smoke-local` PASS。真实 PostgreSQL 18.4/Redis 7.2.14 `smoke-service` 在仓库要求的 uv `0.11.29` 且 localhost 绕过宿主代理时完整退出 0并生成 service trace；此前 `api-auth` 失败已定位为代理返回 HTML 503，`result_committed` receipt 超时在完整重跑中未复现，用户据此裁决该门禁按 PASS 处理。用户明确取消最终 Reviewer 2/3，并对本次 Phase 15 作出一次性 `owner-waived` 裁决；该裁决不构成 Reviewer PASS，也不改变后续 Phase 的默认规则。AC-050/051/055/056/058 已按本地证据勾选，AC-053/054 因 hosted runner 未执行保持未勾选；三个 change 已于 2026-07-22 同步主规格并归档，未发布或推送。
- 已建立 GitHub Actions、GitLab CI、统一 `make ci-*` evidence runner、需求验收矩阵、fail-closed license inventory/report、Conventional Commits release dry-run、checksum 和受保护 promotion/private registry seam。Reviewer 1/2/3 历轮指出的 lock/source/optional closure、验收路径/test/producer、package `basis`、正式 sdist、vendoring license/credential/report、build artifact 生命周期及 NOTICE runtime 边界缺口均已分别保留 red 测试并修复或调整冻结顺序；随后核对历史决策，发现 Phase 13 曾错误地以 redis-py client 版本为由把 Redis server 从已批准的 7.2/BSD-3-Clause 线升到 8.0.1，本轮已新增回归合同并恢复到含 2026 安全修复的 Redis 7.2.14。后续发现的 provider/build 失败回执丢身份、registry 把 uv 零退出误当确认、no-release plan 缺失、需求验收 validator 未作为 hosted required job、AC-006 未真实启动复制模板、no-release 完整 CI DAG 提前绑定凭据/误入 registry、AC-065 错配 SSE producer、vendoring URL query credential 泄漏、editable/virtual runtime 依赖被策略跳过、vendored 绝对路径进入失败报告、AC-050 错配 `test-aggregate`、workspace 根按名称误吞同名第三方身份、AC-001/AC-002 用 pytest 聚合冒充真实安装/构建命令，以及 promotion provider endpoint 允许 URL userinfo 越过无凭据 plan 边界也均已按红测修复。`licensecheck` 对 13 个 runtime identity 的空值/`UNKNOWN` 不再触发串行实时 PyPI 查询，而由与 policy 分离、绑定精确版本官方 JSON 原始字段的观察快照补齐；已有工具观察与快照冲突、身份陈旧或 endpoint 不精确仍 fail closed。GitHub 以 plan output 分流，GitLab 以无凭据 plan 生成动态 child pipeline；`no-release` 只进入无 environment/credential 的零副作用回执 job。license inventory 报告 124 项结果且每项携带策略 `basis`，实际 workspace 根按名称与固定 editable source identity 共同识别，NOTICE/报告保留 PostgreSQL/Redis 版本、安全依据与 Redis server/client license 边界，vendoring 对 allow/deny、URL credential 和非法绝对路径 fail closed；需求验收矩阵显式选择的 92 个 REQ/AC 均映射到具体文件和实际 producer，AC-001 强制 `install`、AC-002 强制 `build`、AC-003 强制 workspace 外 wheel `integration`，AC-011/012/068 强制真实 `smoke-service`，AC-050 强制独立 `acceptance-validate`，AC-065 由 `smoke-local` 完整 local fake run 证据追踪。最近 fresh Reviewer 1 发现 clean hosted `acceptance-validate` 未取得 `install`/`integration`/`build` evidence 以及联合 schema 漏列 license snapshot checksum；本轮均已按 red-first 修复，双 CI 现在显式上传并由终态 job 下载或继承对应 bundle，artifact 名称与解包根目录有静态合同，实际 hosted artifact service 仍未验证。
- 历史中间状态：上一冻结 identity 的 fresh Reviewer 1 Stage 1/2 PASS 后，fresh Reviewer 2/3 继续发现文件级测试映射仍允许空壳节点、AC-012/068 缺少 SQLite 与真实 PostgreSQL 的复合 producer、AC-065 没有正向公开入口测试、GitHub release 多路径 artifact 下载根错误，以及 registry endpoint 允许 query/fragment 夹带 credential。本轮已分别保留 red 测试：92 行测试列改为精确 pytest node，validator 核验节点存在并拒绝 `pass`/`assert True`；AC-012/068 同时绑定 SQLite `test-aggregate` 与 PostgreSQL `smoke-service`，AC-065 新增正向 single-agent fake run；CI contract 固定 release 下载到 `.artifacts`，registry 在 plan/network 前拒绝 userinfo/query/fragment。最终收口以本节首条的 `owner-waived` 裁决为准。
- 历史中间状态：冻结 identity `2d41fa7d.../288435bb...` 的 fresh Reviewer 1 Stage 1/2 PASS；随后 fresh Reviewer 2/3 独立发现 AC-004/005/019/023/026/029/052/061/062 虽已指向精确 pytest node，但部分 node 只检查常量、无关 happy path 或测试替身，未真实验证对应行为，联合 Stage 1 因此 FAIL。本轮已先保留失败契约，再补入真实示例/业务 import 扫描、FakeModelProvider、run/session/trace/eval 默认 tenant、deny 零副作用与 audit、MCP allowlist 零触网、清空真实 key 的 fake eval，以及 API/worker/tool/model/CanonicalEvent 五段关联传播，并将这些审查确认的节点固定进 validator。最终收口以本节首条的 `owner-waived` 裁决为准。
- `ReleaseRecord` 继续只作为 `release-preview/v1` CI artifact，不创建数据库表、migration、repository 或 UnitOfWork；本轮不执行真实 tag、release、registry publish 或 hosted CI。
- 最新语义修复的冻结前全量测试为 `1275 passed, 223 skipped`；上一冻结其余基线为 quality `601 files formatted`、ruff/pyright/import-boundary PASS、integration `11 passed, 23 skipped`、local fake `<5s`、PostgreSQL 18.4/Redis 7.2.14 真实 service smoke、独立 install/integration/build、含 snapshot checksum `279b93649c0b0df145849e2ab15b2c4e4da57abdd6dee8dd615d14931704d35c`、逐项 `basis` 与 runtime 安全/license 边界的 124 项 license report、独立 `acceptance-validate` evidence job `92/92`、lock 207、OpenSpec `31/31`、pre-commit 与 diff check。当前 checkout 无既有 tag，release dry-run 的 first-release 路径为 `0.1.0 -> 0.1.0`、tag preview `agent-harness-v0.1.0`；no-release 路径由全量合同覆盖 `next_version=null`、`tag=null` 且无发布 artifacts。用户已明确接受复用包含最终实现修改的本地证据，不要求因后续状态文档变更重建整套 evidence；该裁决只适用于本次 Phase 15。双 CI 本地 runner 的既有边界不变：GitLab `gitlab-ci-local 4.73.0` 曾真实执行固定 arm64 仓库 `make ci-lock`；GitHub act 曾执行 checkout、setup-uv `0.11.29` 和 `make ci-lock`，但本地 artifact server 不支持 upload-artifact v4 `mime_type`。按用户裁决不再验证本地 artifact upload 或 download，也不把它作为收口失败；GitHub/GitLab hosted runner、远端 environment protection、secret/artifact service 和真实 provider/registry side effect 均保持 `hosted-unverified`。最终 Reviewer 2/3 已由一次性 `owner-waived` 裁决取消，不写成 Reviewer PASS；本地 AC-050/051/055/056/058 已勾选，三个 change 已同步主规格并归档，但未发布。

## [v1.12] - 2026-07-20
### Phase 15 发布记录载体调整

- 根据用户裁决，将 `ReleaseRecord` 明确为版本化、机器可读的 release preview CI artifact，而不是运行时数据库实体；它关联 commit、tag 计划、CHANGELOG preview、release notes、wheel/sdist 与 checksum，并显式表达 `release` / `no-release` 决策。
- Phase 15 不创建 `release_records` 表，不新增 migration、repository 或 UnitOfWork，也不让 release dry-run 连接应用数据库；DEV-PLAN 与三个 active OpenSpec change 据此同步，仍须在契约 1+2 联合审查 PASS 后才进入实现。
- Q-001 已按官方当前文档与双 CI 复用边界决策为 `python-semantic-release==10.6.1` + 仓库 wrapper；真实 promotion 能力保留为受保护 seam，但本轮不对当前仓库或远端执行 commit、push、tag、release 或 publish。

## [v1.11] - 2026-07-20
### Phase 14 状态同步

- 根据 `maintainer-deep-documentation` 的 README、架构/扩展/adapter/context/安全/eval/release 文档、ADR、链接/版本核验和完整本地/真实 service 门禁证据，勾选 AC-049 与 P0 深度文档完成项；不修改运行时、API、schema 或依赖。
- Phase 14 完成声明绑定包含本状态更新的冻结 diff 与 fresh Stage 1/2 review；`maintainer-documentation` 主规格已同步，change 已归档到 `openspec/changes/archive/2026-07-19-maintainer-deep-documentation/`。Phase 15 的 CI、自动版本/tag/CHANGELOG、release dry-run、registry publish 和 需求验收矩阵 仍未开始。

## [v1.10] - 2026-07-19
### Phase 13.9 归档

- 将 `sse-event-streaming` 的三个 delta specs 精确同步到 `canonical-events-artifacts`、`service-app-shell` 与新增的 `sse-event-streaming` 主规格，保留既有主规格内容。
- `sse-event-streaming` 已以 17/17 tasks 归档到 `openspec/changes/archive/2026-07-19-sse-event-streaming/`；该归档快照中无 active change，不代表 push、发布或部署。

## [v1.9] - 2026-07-19
### Phase 13.9 状态同步

- 根据 `sse-event-streaming` 实现与验证证据，完成 RUN-006 SSE transport、`Last-Event-ID` 恢复、CLI-EVT-001 canonical NDJSON、统一授权 EventSink reader 与首 frame P95 门禁；WebSocket、跨 run multiplex、外部 broker gateway 和 event retention 仍不在本次 P0 范围。
- 勾选 AC-017、AC-038、AC-066；真实 PostgreSQL/Redis service smoke 已覆盖 SSE 初始读取、exclusive resume、terminal EOF、非法第二 cursor 与零业务副作用，3 名 fresh reviewer 已完成 Stage 1/2，active change 保持未归档并进入 `ready-to-archive`。

## [v1.8] - 2026-07-18
### 规格维护

- 将既有 `budget.max_tokens_per_run` / `budget.max_cost_usd_per_run` 在 P0 预发布阶段收紧为 parent execution tree 共享硬上限：root direct model/embedding、获准 delegation 及 child allocation 统一竞争同一 durable owner ledger；公开字段与 `/api/v1` shape 不变，cost 为 `null` 时只关闭 shared cost 维度。
- 补齐 shared-budget 安全边界：tenant-scoped keyed request fingerprint 只能通过 typed settings 的 env / Docker secret file 边界加载，启动时 fail closed；任何 runtime、migration、evidence、错误或配置快照不得持久化或回显密钥原值。
- 补齐 `0016` 历史迁移合同：DDL 前校验全库 parent-child 拓扑，拒绝嵌套、孤儿、循环、跨租户或 delegation relation 不唯一；未封闭 tree 只能使用独立 durable immutable source evidence 回填，cost-enabled snapshot 的必需价格不得为 null。
- 统一 usage application UoW 错误优先级，并要求未封闭 shared-budget claim/allocation、unknown 或 needs-review 状态阻止 parent terminal；RUN-002 最终以 `RunDetailResponse` 为唯一合同，消除 active changes 归档投影冲突。

## [v1.7] - 2026-07-16
### 规格维护
- 收紧 Run API 公开契约：按实际 operation 区分 response status，RUN-002 原子切换为 `RunDetailResponse`，并要求 route、schema、OpenAPI 与双向 drift test 保持一致。
- 完成配置与 secret file 边界定义：四类 application startup 入口统一 fail closed；Docker secret file 受信根目录、普通文件、大小、direct/file 冲突和错误脱敏规则成为可执行验收，异常链及 traceback frame locals 不得泄漏原值。
- 固定 canonical run trace 与 evidence 关联：run 创建前生成唯一 trace，传播到 checkpoint/resume、worker、approval、CanonicalEvent、usage 与 delegation；历史 shape 通过前滚迁移收敛，event-id 重放必须核对除 sink seq 与重建 timestamp 外的完整稳定语义。
- 增加 model/embedding provider-neutral usage 契约：统一 started/final evidence、稳定语义调用槽位、token/cost/latency 校验、durable settlement/outbox、terminal 前恢复顺序和 local `<5s` 性能验收；embedding cache 也纳入同一证据边界。
- 增加真实受控 delegation 契约：明确 edge/policy/tenant/cycle/depth/budget/idempotency 前置门禁、parent 级原子预算与 event capacity 预约、`0015` migration、local/Redis worker 恢复、durable parent aggregation、RUN-002 detail 以及 unknown/非法 usage 的 needs_review 处理；RUN-002 对已持久化但尚未结算的 child 仍返回身份与活动状态，只有确实没有 child relation 时才返回 null，完成与活动 child 并存时不得遗漏且预算状态保持 incomplete。
- 校正 CanonicalEvent 固定目录为 39 种，纳入 `artifact.created` 和四种 delegation 生命周期事件；固定 delegation 的最多三条顺序、稳定 event id、parent run/trace/source agent 归属、internal/non-terminal 可见性、阶段 payload 与敏感字段禁止项。
- 强化 terminal 不变量为双向约束：只有 `run.completed|run.failed|run.cancelled` 可以且必须设置 `terminal=true` 和 `visibility=public`，其他事件必须 non-terminal；不一致 envelope 必须在 seq、容量、artifact 和 fan-out 副作用前拒绝。
- 更新 Phase 13.5、13.6、13.6A、13.7 与 13.8 的实施状态：已完成项保持 active 并只到 `ready-to-archive`，不代表归档、发布或部署；新增事件目录与 terminal 边界已由 AC-067 和 `agent-delegation-execution` task 1.3 固定，Phase 13.8 的真实委派、durable parent aggregation、恢复与幂等边界已通过完整门禁和最终代码 1+2；生产代码及历史大测试已按职责和行为域拆分，公开 facade、ORM 注册顺序、`typing.Literal` 身份和测试收集完整性均有回归证据。

## [v1.6] - 2026-07-12
### 基线审查修正
- 根据 Phase 1-13 基线审查和用户裁决，保留真实 delegation 与 SSE transport 的 P0 承诺；二者必须在 Phase 14/15 前通过聚焦 OpenSpec change 实现，当前保持未完成。
- 将 P0 secrets 范围收窄为 env / Docker secret file 配置消费与全链路脱敏；抽象 SecretProvider、Vault/KMS adapter 明确放入 P1，并新增可执行验收标准。
- 补充 model/embedding token、cost、latency trace 和性能 NFR 的可执行验收；这些行为缺口仍保持未完成，不以现有 DTO 或 JSON events seam冒充完成。
- 同步 17 个有直接实现与合同测试证据的 AC 状态；新增 RUN-006 后 AC-017 重新打开。AC-008 仅有 loader 证据；AC-050 改为当前可审计的 REQ/AC -> production -> test evidence 追踪，同时保留新 change 必须先有 red 证据的过程门禁。
- 明确 CI quality job 分别执行 `make quality` 与 `make test`，不再把 unit/contract tests 误写成 `make quality` 单命令的当前职责。
- 补齐 Phase 12.5 的 EvalDatasetSplit、EvalExperiment、HarnessAcceptance 数据实体，并明确持久化业务实体必须直接携带 tenant_id。
- 明确 Graph workflow 和 Redis session cache 为 P1 可选能力，修正架构 validator 的两个受控 crossing warning 记录；Phase 14、15 继续保持未完成。

## [v1.5] - 2026-07-12
### 状态同步
- 根据用户显式归档指令，将 Phase 13 三个已完成 change 按依赖顺序同步到长期主规格并归档；仅同步生命周期状态，不修改需求语义。
- 当前无 active OpenSpec change；Phase 14 深度维护文档与 Phase 15 CI/release automation 验收继续保持未完成。

## [v1.4] - 2026-07-11
### 状态同步
- 根据 Phase 13 三个 active change、真实 PostgreSQL/Redis/DBOS/Compose 证据与 fresh review 结果，勾选 AC-059、AC-060、AC-062 及分进程部署边界完成项；仅同步验收状态，不修改需求语义。
- Phase 14 深度维护文档与 Phase 15 CI/release automation 验收继续保持未完成；三个 Phase 13 change 保持 active，等待用户决定是否归档。

## [v1.3] - 2026-07-10
### 状态同步
- 根据 Phase 12 三个归档 change、最终测试/审查证据与提交历史，勾选已完成的模板运行、OpenAPI、approval/tool、context/retrieval、trace/eval、四示例、README 和 vendor boundary 验收项；未修改需求语义。
- Phase 12.5 的 experiment/holdout/harness acceptance、Phase 13 的分进程，以及 Phase 14-15 的深度文档、CI 与 release 验收继续保持未完成。

## [v1.2] - 2026-07-10
### 调整
- 同步 Phase 12 `AgentExecutor` 契约：手工新增 agent 与 scaffold 生成路径都必须在 `agent.py` 暴露公共 protocol 入口，并在 `config.yaml` 声明 package-local executor reference。
- 明确 executor 缺失、越过 agent package、module/callable 无效或不符合 protocol 时 registry 整体拒绝，不允许回退到固定 fake output。
- 明确 executor reference 属于私有加载配置，不进入 public descriptor、API 或 CLI payload。
- 细化 `AC-013` approval continuation：approval-gated run 进入 waiting 并持久化 checkpoint/approval 后，必须能在进程重启、使用同一持久化 storage 重建 registry、executor resolver、orchestrator 和 approval service 的条件下，经私有 lease、绑定 `ApprovalGrant` 与 runtime 内部 resume 恢复原 continuation；公开 resume token 不构成执行授权。
- 明确公开 `RUN-005` 只恢复普通 checkpoint；approval-gated checkpoint 直接提交原始 token 必须在消费 token 或调用 handler 前返回稳定冲突，真实 approve 通过 `APR-002` 原子仲裁且仅在确定性结果和 run terminal 落库后公开为 approved。
- 补全 approval owner 硬退出恢复：raw claimed lease 只有在可配置 timeout 到期且不存在 execution claim时才能由真实 resolve 重试换发 fencing id；活跃 lease、已有 claim与旧 owner均不得被并发抢占或继续执行。

## [v1.1] - 2026-07-06
### 调整
- 同步最新架构图语义：补充 Agent Loop、HITL 回边、SSE/WS 流式回传、信任边界和 Prompt / 策略版本回溯要求。
- 明确 P0 InputGuardrail 契约：用户/API/CLI 输入进入 run 前执行轻量过滤、注入风险检测、trust marker 标注，并把检查结果写入 trace/audit。
- 明确 MCP tool output、tool output、retrieval chunk 默认作为 untrusted input 处理；进入模型上下文前必须保留 source_ref、trust_level、artifact_ref 和截断信息。
- 将 REQ-012 扩展为“模型、预算、上下文组装与 embedding”，新增 ContextAssembler 对 history、retrieval、tool output、artifact refs、token budget 和 fallback decision 的收口责任。
- 扩展 CanonicalEvent P0 事件类型，加入 input.guardrail.* 与 context.assembly.* 事件，并要求 local/jsonl 只记录摘要、来源、可信级别和截断元数据。
- 新增 GuardrailCheck 与 ContextAssembly 数据模型条目，补充上下文组装和信任边界的数据规则。

## [v1.0] - 2026-07-05
### 新增
- 新增 Agent Harness Layer 初始 Product Spec。
- 新增后端服务型 agent 脚手架产品定位，明确不是单一 demo agent、不是完整 SaaS 管理台。
- 新增 `uv workspace` monorepo、`packages/agent-harness` 可打包核心库、`templates/service-app` 后端模板范围。
- 新增 Pydantic AI 默认生态和 `agent_harness` 适配层策略，默认依赖上游包，不 vendoring 全源码。
- 新增 DBOS P0 durable execution、SQLite local checkpoint、Temporal P1 adapter 策略。
- 新增 SQLAlchemy 2.0 typed declarative + Alembic + Repository + Unit of Work 存储策略。
- 新增 PostgreSQL/Redis service profile 和 SQLite/filesystem local profile。
- 新增多 agent registry 与受控 delegation 规格。
- 新增默认租户、IdentityContext、API Key/Bearer Token 认证规格。
- 新增 PolicyEngine、危险动作可配置审批、HITL CLI/HTTP 审批和审计要求。
- 新增 FileTool、ShellTool、MCP client、RetrievalProvider、EmbeddingProvider、ModelRouter 能力。
- 新增 BM25 P0、PGroonga P0 optional adapter、pgvector P0 optional adapter、hybrid retrieval + RRF 接口。
- 新增 CanonicalEvent 事件模型、SSE/CLI/local-jsonl/OTel adapter 和事件 resume 规则。
- 新增 Observability 转换层，明确 local/jsonl 永久保留，Logfire 推荐，Phoenix/Langfuse 走 adapter。
- 新增 Eval Gate 转换层和 trace -> eval case -> human review -> approved dataset -> eval run -> score sink -> observability provider 闭环。
- 新增四个 P0 薄样例 agent：RAG assistant、ticket triage、repo analyst、dev assistant。
- 新增 README 和深度文档要求，明确目录结构树、职责和禁止跨边界规则必须写入 README。
- 新增 TDD 强约束、unit/contract/integration/eval/smoke 测试结构、ruff、pyright、pytest、coverage、pre-commit。
- 新增 GitHub Actions 与 GitLab CI 等价门禁。
- 新增 release automation / tag / CHANGELOG generation 为 P0，包括 SemVer、Conventional Commits、wheel/sdist artifact、私有发布路径。
- 新增 Apache-2.0 license、NOTICE、引用声明和 license check 要求。
- 新增 P0 未来微服务拆分基础要求：P0 不强制全量微服务化，但必须定义 API、runtime worker、model/tool gateway、storage、event/observability 的稳定边界和分进程 service profile 验收。
