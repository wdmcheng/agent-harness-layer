## MODIFIED Requirements

### Requirement: AgentRegistry 加载并校验多个 agent descriptor
系统 SHALL 从受控 agent config 目录加载多个 `AgentDescriptor`，并拒绝重复 `agent_id`、无效 schema 或缺少必要字段的配置。每个 `config.yaml` MUST 声明 `agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model.provider`、`model.deployment_id`、`model.allowed_models`、`model.default_model`、`model.fallback_models`、`budget.max_tokens_per_run`、`budget.max_cost_usd_per_run`、`tool_allowlist`、`eval_dataset` 和 `delegation_edges`；default/fallback models MUST 是 allowed models 的子集且不得重复。public descriptor SHALL 只暴露 `agent_id`、`version`、`name`、`description`、schema refs、相对 `config_ref`、tool policy summary、model policy summary、budget summary、eval dataset ref 和 delegation target ids；MUST NOT 暴露本地绝对路径、provider secret、endpoint、callable 或 provider client。

#### Scenario: 列出已配置 agent
- **WHEN** 调用方通过 CLI 或 API 请求 agent 列表
- **THEN** 系统返回已配置 agent 的 public descriptor 字段和不含 endpoint/credential 的 deployment/model policy summary，且不暴露本地路径、provider secret 或内部对象

#### Scenario: Descriptor 字段契约完整
- **WHEN** registry 加载 smoke agent config
- **THEN** descriptor 包含 `agent_id`、`version`、输入/输出 schema refs、相对 `config_ref`、deployment/model 策略、预算、工具白名单摘要、eval dataset 和 delegation edge 列表

#### Scenario: 重复 agent_id 被拒绝
- **WHEN** registry 加载到两个相同 `agent_id` 的配置
- **THEN** registry 失败并返回稳定错误码，错误详情包含冲突的 `agent_id`

#### Scenario: 无效 agent config 被拒绝
- **WHEN** agent config 缺少必要字段、字段类型不合法，或 default/fallback models 扩大或脱离 allowed models
- **THEN** registry 失败并返回 registry validation error，不创建部分可用的脏 registry

#### Scenario: Scaffold 生成严格且可离线运行的 fake Agent 配置
- **WHEN** 调用方执行 `agent-harness scaffold agent` 生成新 Agent package
- **THEN** 生成的 `config.yaml` 显式写入 `model.provider=fake`、`model.deployment_id=fake_default`、`model.allowed_models=[fake-scaffold]`、`model.default_model=fake-scaffold` 与空 fallback；local/service profiles 的 `fake_default` deployment 允许该模型，生成包通过正式 Registry 校验和离线 runtime 执行，且不读取真实 credential 或访问网络

### Requirement: ModelRouter 通过 provider-neutral 接缝执行路由和预算判断
系统 SHALL 通过 `ModelProvider` interface 和 `ModelRouter` 选择 deployment/default/任务级模型，执行 timeout、有限 fallback 和预算估算，并为 deployment、provider、budget 配置变更提供显式 restart/reload seam。`provider_kind` 是 deployment 的协议/构造族；受控真实非流式文本调用中，`AgentModelPolicy.provider`、可选 `ModelRequest.provider` assertion、`ModelRoutePlan.provider`、公开 evidence `provider` 与绑定 `ModelProvider.provider_id` MUST 逐字等于 deployment `provider_kind`，仅允许 `fake|openai-compatible`。`pydantic-ai` 是私有 adapter 实现名，MUST NOT 成为 route/provider identity。Router MUST 先计算 deployment allowed models 与 Agent model policy 的交集，再让 `ModelRequest.deployment_id`、`model` 和 capability 仅作进一步缩权；settings 只要含非 fake 受控 deployment，公共 `plan()`/`route()` 缺少 Agent model policy 就 MUST 返回 `model.route_not_allowed`，MUST NOT 回退 legacy/fake 路由。request 中兼容保留的 provider 字段只能作为上述相等断言，MUST NOT 选择 provider、endpoint 或 credential。`ModelRequest` MUST `extra=forbid` 且不声明 endpoint/credential 字段，因此敏感 override 在 DTO 边界稳定返回 `model.request_invalid`，不得伪装成 router decision。Router MUST 在 soft policy/fallback/approval、预算预约与 provider 副作用前产出 immutable `ModelRoutePlan`，冻结 deployment/provider/model、canonical base URL/origin、endpoint policy/default catalog ref/version/digest、model catalog ref/version/digest 与解析的 request-shape/strategy/envelope/price、completion classifier ref/version、credential ref identity、capability、prompt UTF-8 bytes、trusted input bound、output cap、每 attempt 可信 token/cost 上界、`max_attempts`、调用级 reservation 上界、timeout/retry/bulkhead 与 snapshot schema version；decision、retry 与 bulkhead嵌套对象也 MUST 是不可变 typed DTO，禁止以 frozen 外层包裹可变 mapping。动态 hard eligibility 通过后 MUST 先执行 soft policy/fallback/approval并写既有 `policy.decision` audit；deny 或 approval-required 未获批准时 reservation/permit/client/mark/network 全部为零，approval 不得提高、重置或覆盖 shared hard limit。

#### Scenario: Fake model 不需要真实 API key
- **WHEN** local profile 使用 fake deployment 运行 tests 或 smoke
- **THEN** 模型调用成功返回可预测结果，且不读取真实 provider key、不创建真实 provider client或访问网络

#### Scenario: 三层模型范围只取交集
- **WHEN** deployment、Agent descriptor 与 request 声明不同模型范围
- **THEN** route plan 只选择 deployment allowlist ∩ Agent allowed models 中由 request 选定或 Agent default/fallback 决定的模型，且调用后修改原 settings、descriptor、request 或 mapping 都不能改变该 plan

#### Scenario: 受控路由缺少 Agent policy
- **WHEN** Router settings 已包含非 fake 受控 deployment，但调用方通过公共 `plan()` 或 `route()` 没有提供 Agent model policy
- **THEN** Router 返回 `model.route_not_allowed`，provider call count 为零，不得按 request/config 回退 legacy/fake 路由；完全没有受控 settings 的既有 fake 兼容入口保持离线可用

#### Scenario: 请求尝试扩大路由范围
- **WHEN** 已通过 DTO 校验的 request 选择未知 deployment/model、provider 断言不匹配，或请求 Agent/deployment 未共同允许的 route
- **THEN** router 返回 `model.route_not_allowed`，在预算预约、授权成功 evidence 与 provider call 前失败；允许的本地拒绝 evidence 必须脱敏且不得声称 route 已授权

#### Scenario: Provider assertion 与绑定 adapter 使用同一身份
- **WHEN** Agent、request、deployment 或 composition 绑定的 adapter 中任一 provider identity 与 deployment `provider_kind` 不一致
- **THEN** registry/composition 或 router 在预算预约、client 构造和 provider call 前返回 `config.invalid` 或 `model.route_not_allowed`；一致的 `openai-compatible` route 不得因 adapter 使用 Pydantic AI 实现而改写成 `pydantic-ai`

#### Scenario: Prompt、输出 cap 与价格公式在副作用前冻结
- **WHEN** 真实 request 的 prompt 超过 `max_prompt_utf8_bytes`，`max_output_tokens` 不在 `1..deployment.max_output_tokens`，catalog strategy 缺失，或配置的 token/cost bound 低报、高报、溢出或与冻结价格公式不一致
- **THEN** router 以 `budget.reservation_rejected` 在 reservation、Bulkhead、client lease 获取/构造和 provider call 前失败；本调用的 reservation count、client-construction delta 与 network/provider call count 均为零；合法 route 冻结 input bound/output cap，adapter 只能把同一 output cap 传给 provider

#### Scenario: 请求 capability 不受支持
- **WHEN** 已通过 DTO 校验的 request 请求 route 未声明的 capability
- **THEN** router 返回 `model.capability_unsupported`，在预算预约和 provider call 前失败

#### Scenario: Soft policy 与 approval 早于 reservation
- **WHEN** hard eligibility 已通过，但 soft policy deny、触发 approval-required 或尚未取得批准
- **THEN** runtime 以 bound identity 对 exact `action=model.invoke`、`resource=agent:<agent_id>:model` 和脱敏 route/catalog/bound context 执行 PolicyEngine；policy audit 可持久化，但不建立 reservation、不取得 permit、不获取或构造 client、不写 durable mark且不发网络；批准只能继续或缩小冻结 intent，不能提高、重置或覆盖 owner shared hard limit

#### Scenario: Endpoint 或 credential override 在 DTO 边界被拒绝
- **WHEN** 原始请求携带 `endpoint`、`base_url`、`credential`、`credential_ref` 或其他未声明字段
- **THEN** request validation 返回 `model.request_invalid`，router 不执行且没有预算、model policy audit 或 provider 副作用

#### Scenario: 预算超阈值产生可追踪 fallback decision
- **WHEN** 模型调用预计 token 或 cost 超过配置阈值
- **THEN** `ModelRouter` 只从冻结交集内返回可追踪的 fallback / policy-needed decision summary，包含估算值、阈值和选定动作，fallback 重新经过 hard eligibility 与预算判断

#### Scenario: Cost hard limit 缺少可信价格
- **WHEN** cost hard limit 启用但候选 route 缺少 input/output price、catalog ref 或 version
- **THEN** 调用以 `budget.reservation_rejected` 在 provider 副作用前失败，cost 不被当作 0，且不自动切换 fake

#### Scenario: 业务 agent 不直接 import Pydantic AI
- **WHEN** import boundary check 扫描 runtime core、template app 和业务 agent
- **THEN** `pydantic_ai`、`openai` 和 SDK client 类型只允许出现在仓库相对前缀 `packages/agent-harness/src/agent_harness/adapters/` 下的批准 vendor boundary 或不进入生产扫描面的测试替身中；路径其他位置仅出现名为 `adapters`/`integrations` 的片段不得获得豁免，当前没有额外批准的 integration root

### Requirement: Run budget snapshot 在创建时冻结
Root run SHALL 在创建且任何业务副作用前冻结 tree budget snapshot。该 snapshot MUST 区分 owner envelope 与 agent sub-snapshots：owner envelope 保存 `max_tokens_per_run`、`max_cost_usd_per_run`、cost-enabled 状态、registry/config/catalog versions、snapshot ID 与 `schema_version`；root agent 与当时显式允许的单层 delegation targets 各自保存独立 descriptor version、完整 deployment/model policy、target budget ceiling、允许 provider/model routes、每条真实 route 的非敏感 canonical base URL（包含已校验安全 path）、endpoint policy ref/version/digest、model catalog ref/version/digest 及解析的 request-shape/strategy/envelope/price、completion classifier ref/version、条件化 price source refs/versions、每 attempt token/cost 上界和 `max_attempts`。Cost enabled route MUST 冻结非空 price-source identity 与 cost bounds；cost-disabled route 的两项价格、price-source identity 与全部 cost bounds MUST 为 null。Child MUST 继承同一 owner snapshot ID 与 shared hard limits，并按自身 target `agent_id` 使用 root 时刻冻结的对应 sub-snapshot，不得继承 source agent descriptor 或读取 reload 后的 target 配置。Target ceiling 只能进一步收紧 owner 已启用维度，不能提高 shared hard limit 或重新启用 owner 已关闭的 cost 维度。Reload MUST 只影响新 root run。Fallback MAY 在当前 agent 对应的 frozen route/endpoint/model-catalog/classifier/price/attempt-bound sub-snapshot 内按实际 route 重算 trusted reservation，但 MUST NOT 修改该 run hard limit或使用 reload 后配置、endpoint path、model catalog、classifier 或价格。

新建 snapshot SHALL 使用 `schema_version="budget-tree-v2"`，且 `snapshot_id` MUST 以 `budget-tree-v2:` 开头；恢复时 MUST 同时校验 schema version、id prefix 与 exact v2 required shape，不得从当前 settings 补齐缺失值。既有 `budget-tree-v1:` snapshot 只在没有 `schema_version`、满足旧版完整 required shape 且其中所有旧 model policy provider 均为 `fake` 时，允许仅从旧 payload 投影 synthetic legacy fake route。任何 partial、混合版本、未知版本、v1 real provider 或损坏快照 MUST fail closed。

#### Scenario: Reload 不改变在途 run
- **WHEN** root run 已冻结 budget snapshot，随后 registry/provider/budget/price 配置 reload
- **THEN** 该 run 及其 child 继续使用原 hard limits、config version、deployment/model、canonical base URL/path、endpoint policy identity、model catalog ref/version/digest 与解析值、completion classifier identity、attempt bounds，以及 cost enabled 时冻结的 price source/version；cost-disabled route 继续保持 price source/version 为 null，新 root run 才使用 reload 后 snapshot

#### Scenario: Fallback 重算 reservation 但不改上限
- **WHEN** 在途 run 按 frozen policy 选择另一个允许的 fallback route
- **THEN** router 使用该 route 在 frozen model catalog ref/version/digest、attempt bounds，以及 cost enabled 时解析的 price source/version 下重算 trusted reservation；cost-disabled route 保持 price source/version 与 cost reservation 为 null，并继续受原 frozen parent hard limit 约束

#### Scenario: 跨 agent child 使用冻结的 target sub-snapshot
- **WHEN** source agent 委派到 descriptor/model policy/budget 与 source 不同、但在 root 创建时显式允许的 target agent
- **THEN** child 继承同一 owner snapshot ID 与 shared hard limits，同时使用 snapshot 内该 target 自己的 descriptor/deployment/model-policy/route/model-catalog/price/attempt-bound 版本；source/target descriptor 不同不构成冲突，target ceiling 只能进一步收紧已启用 owner 维度

#### Scenario: Target reload 不改变既有 tree
- **WHEN** root 创建后 target descriptor、deployment/model policy、budget 或 price catalog reload，再创建或恢复该 target child
- **THEN** child 仍使用 root tree snapshot 中的 target sub-snapshot；未在该 snapshot 中冻结的 target 或 route 在 provider/child/queue 副作用前拒绝，新 root 才使用 reload 后版本

#### Scenario: 新 root run 写入 v2 快照
- **WHEN** 系统为新 root run 创建共享预算树
- **THEN** snapshot 使用 `budget-tree-v2:` id 和 exact v2 payload，私有地冻结恢复所需 canonical base URL/path、endpoint policy identity、model catalog ref/version/digest 与解析的 request-shape/strategy/envelope/price、completion classifier identity、模型策略与预算上界；不包含 credential value 或 classifier header 原值，公开 descriptor/evidence 也不暴露完整 endpoint URL

#### Scenario: 同 origin 不同 path 的 reload 不改变旧 route
- **WHEN** 已冻结 route 的 base URL 是 `https://api.example.test/v1/a`，reload 后同 deployment/current settings 改为同 origin 的 `/v1/b`
- **THEN** 旧 run 只从 v2 snapshot 恢复 `/v1/a`，不得读取 current `/v1/b`；若 snapshot 缺 path 或冻结 credential ref 已无法安全转发到旧 origin，则在 provider 副作用前 fail closed，SQLite 与 PostgreSQL 行为一致

#### Scenario: 完整 v1 fake 快照兼容恢复
- **WHEN** 数据库中存在 `budget-tree-v1:`、无 `schema_version`、形状完整且 provider 为 `fake` 的旧快照
- **THEN** runtime 只使用该快照已有字段投影 synthetic legacy fake route，不读取当前 deployment/default/provider 配置来改变历史行为

#### Scenario: 部分或混合版本快照 fail closed
- **WHEN** snapshot id 与 schema version 不匹配，v1 声明真实 provider，v2 缺字段，或 payload 只能借助当前 settings 才能补齐
- **THEN** 恢复以 `budget.reservation_rejected` 和安全 `snapshot_invalid`/`needs_review` reason 停止，provider call count 为零，SQLite 与 PostgreSQL 行为一致

## ADDED Requirements

### Requirement: 模型 soft policy 与审批复用既有 durable continuation
模型调用 SHALL 在 hard eligibility 后、reservation 前通过既有 `PolicyEngine.evaluate(PolicyCheck)`：actor MUST 是 runtime 绑定的 `IdentityContext`，action MUST 为 `model.invoke`，resource MUST 为 `agent:<agent_id>:model`，context MUST 只包含 tenant/run/agent/request/trace、冻结 route/model-catalog identity、reservation bounds 和 soft-limit decision，不含 prompt、secret 或完整 URL。`PolicyEngine` 的既有 `AuditService` `policy.decision` 记录及 `audit_ref` SHALL 是唯一授权成功审计；MUST NOT 新增 model authorization CanonicalEvent，也不得占用 model event capacity或声称 provider 已开始。

`require_approval` SHALL 复用既有 `AgentApprovalRequest`、`policy_approval` checkpoint、`ApprovalRecord`/resolution lease、`ApprovalGrant`、`ApprovalService` 与 `RunOrchestrator.resume_run()`。等待状态 MUST 冻结 action/resource、canonical `ModelRequest` arguments hash、tenant/identity/agent/run/request/trace 与 checkpoint token；未批准时 reservation/permit/client/mark/network 均为零。ApprovalService MUST 只从 durable record/lease 构造 grant，runtime MUST 在调用 model approved seam 前校验 approval id、lease、tenant、identity、agent、run、action、resource、arguments hash、状态与单次 lease。`BoundModelInvocationService.complete_approved()` MUST 只接受已验证 grant，不得向业务调用方暴露或信任 `soft_approved: bool`；它只跳过同一 soft decision，稳定 operation identity 绑定 approval id，并重新执行 hard route/model catalog/current owner balance。Mismatch、stale、duplicate/replay MUST fail closed；获批 continuation 最多触发一次 provider 调用，crash/replay 只恢复同一 settlement。证据 MUST 复用既有 `policy.decision` audit、`approval.required|resolved` 与 `run.resumed`。

#### Scenario: Policy allow audit 不伪装 provider side effect
- **WHEN** exact model PolicyCheck 返回 allow，随后 client factory 在 durable mark 前失败
- **THEN** audit 保留 decision=allow 与安全关联字段，但不新增 CanonicalEvent、不声称 provider 已开始；reservation/permit 被回滚且 mark/network 为零

#### Scenario: Require approval 创建 durable waiting 状态
- **WHEN** model policy 返回 require_approval 或 soft threshold 要求人工批准
- **THEN** runtime 创建绑定 exact request hash 的既有 approval/checkpoint并停止，model reservation/permit/client/mark/network 为零；deny 只保留 policy audit而不创建 approval

#### Scenario: Bound grant 续接恰好一次
- **WHEN** ApprovalService 从已批准 durable lease 构造全绑定 grant并恢复相同 checkpoint
- **THEN** runtime 校验全部绑定与单次 lease，approved seam 重算 hard route/catalog/current balance后最多执行一次 provider 调用；相同 crash/replay 只恢复同一 settlement

#### Scenario: Mismatch、stale 或重复 grant fail closed
- **WHEN** grant 的 approval/lease/tenant/identity/agent/run/action/resource/hash 任一不匹配、状态陈旧或已消费
- **THEN** continuation 在 reservation/client/network 前拒绝，不允许调用方以 bool 或原始 token绕过 soft gate

### Requirement: Runtime composition 通过 lazy client factory 注册受控非流式 provider
Composition root SHALL 从已验证 settings 和 registry 构造 provider-neutral router：`fake` deployment 注册离线 provider；首个真实 deployment通过唯一批准的 Pydantic AI adapter 注入已验证 typed endpoint/model catalogs、credential/base URL/deadline/retry/bulkhead/completion-classifier 的私有 lazy `ControlledOpenAIClientFactory` blueprint，而不是在 startup 预构造 async client。每次调用 MUST 严格按 immutable route 动态 hard eligibility → soft policy/fallback/approval + 既有 `policy.decision` audit → reservation → Bulkhead permit → factory 获取/构造绑定 frozen route 的 client lease → durable `side_effect_started` mark → send 执行；deny 或 approval-required 未批准时全部后续副作用为零，approval 不得放大 shared hard limit。SDK client 构造不得联网，失败时必须按 not-started 回滚 reservation/permit，使最终 active reservation、mark、network delta 均为零。Provider/factory/已构造 client lease 生命周期 MUST 由 API、worker 和 CLI composition 统一幂等关闭；SDK Agent/result/client 不得进入业务 Agent、checkpoint、descriptor 或 public DTO。

锁定 `openai==2.44.0` 即使显式传 `api_key/base_url` 仍会检查 `OPENAI_ADMIN_KEY`、`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_WEBHOOK_SECRET` 与 `OPENAI_CUSTOM_HEADERS`。Factory MUST 在不修改进程全局环境的前提下显式传 typed `api_key`、空 admin/org/project/webhook、冻结 base URL 和空 default headers，并把 client 置于 `trust_env=False`、`follow_redirects=False` 的私有出站 transport 后。Transport MUST 在 socket send 前 exact 校验冻结 origin/base path，并从 typed secret/adapter 常量/stable attempt identity 重建封闭 header allowlist；ambient 或 SDK 合并的 Authorization、OpenAI-Organization、OpenAI-Project、cookie、proxy auth 与其他非 allowlist header MUST 被删除，不能改变 client identity、计费归属、endpoint 或请求。Provider 原生 ambient env 可被依赖内部检查，但 MUST NOT 形成第二条配置/身份/出站路径。

#### Scenario: 有效 deployment 构造真实 provider
- **WHEN** startup 取得完整的真实 deployment、匹配 Agent policy 和已解析 typed credential
- **THEN** composition 只注册按 `deployment_id` 寻址的 lazy provider/factory，client-construction count 为零；首个合法 route 完成 hard eligibility、policy allow audit、reservation 与 permit 后才构造 client lease，随后按 mark/send 顺序执行一次非流式 text completion 并返回 provider-neutral text、usage、latency 和 route evidence

#### Scenario: 启动失败没有部分 runtime
- **WHEN** 任一真实 deployment 的 endpoint、credential、capability、价格或 policy 无效
- **THEN** API、worker 与 CLI composition 在接受 run、连接 provider 或发布业务 evidence 前结构化失败，不留下只注册部分 provider 的 runtime

#### Scenario: OpenAI SDK ambient env 不能改变受控请求
- **WHEN** 进程同时注入 `OPENAI_API_KEY`、`OPENAI_ADMIN_KEY`、`OPENAI_BASE_URL`、`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_WEBHOOK_SECRET` 与含伪造 Authorization/OpenAI/custom header 的 `OPENAI_CUSTOM_HEADERS`，而合法 route 使用另一组 typed credential 与冻结 endpoint
- **THEN** client/transport double 观察到 endpoint、Authorization、organization/project、自定义 header 和 client identity 只来自冻结 typed plan/allowlist；ambient key/header/origin 均未出站，进程全局环境未被修改

#### Scenario: 动态拒绝不触发 lazy client
- **WHEN** deployment 静态配置有效，但某次 request 的 prompt/output/strategy/price/checked formula 不满足动态 hard eligibility
- **THEN** router 在 reservation 和 factory 前返回 `budget.reservation_rejected`，本调用 reservation count、client-construction delta 与 network/provider call count 均为零；已有其他 route 的 cached client 不得被获取或用于该调用

#### Scenario: Client factory 失败不伪造 provider started
- **WHEN** 合法 route 已取得 reservation 与 permit，但 factory 获取/构造 lease 失败
- **THEN** runtime 回滚 reservation/permit并关闭部分资源，不写 durable mark、不发网络；先前 policy audit 只保持其原始 allow decision，不得改写为 provider 已开始，最终 active reservation、mark 与 network delta 均为零

#### Scenario: Composition 关闭网络资源
- **WHEN** API lifespan、worker 或 CLI runtime 退出或启动中途失败
- **THEN** 所有已创建的 async provider client 被幂等关闭，然后再完成其余资源清理，不泄漏连接或 secret
