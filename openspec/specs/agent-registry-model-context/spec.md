# agent-registry-model-context Specification

## Purpose
定义 Agent Registry、模型路由、ContextAssembler 与 Embedding provider/cache 的长期契约，使多 agent 发现、provider 边界、上下文组装和 embedding 复用有稳定 public seam。
## Requirements
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

### Requirement: Agent executor reference 受控加载且不公开
每个 agent config SHALL 显式声明相对 Python module/callable executor reference；`AgentRegistry` MUST 只解析位于该 config 所属 agent package 内、实现 `AgentExecutor` protocol 的入口，并 MUST NOT 在 public `AgentDescriptor`、API response、CLI list 或序列化 payload 中暴露 callable、module object 或本机绝对路径。Executor contract 生效时 MUST 同步迁移现有 basic/fake agent 与测试 fixture；缺少 executor 的 config MUST 形成结构化 validation error，不得隐式回退到固定 `fake-ok`。

#### Scenario: 合法 executor 被内部 resolver 加载
- **WHEN** registry 加载一个 executor reference 指向该 agent package 内的 callable
- **THEN** internal resolver 返回符合 `AgentExecutor` protocol 的执行入口，public descriptor 字段保持不变

#### Scenario: 越界或无效 executor 整体拒绝 registry
- **WHEN** executor reference 使用绝对路径、越过所属 agent package、引用缺失 module/callable 或对象不符合 protocol
- **THEN** registry 返回结构化 validation error，不加载部分可运行 registry，也不执行引用目标

#### Scenario: 缺少 executor 不走 legacy fallback
- **WHEN** registry 加载现有或新增的 agent config 而该 config 没有显式 executor reference
- **THEN** registry 返回结构化 validation error，不注册该 agent，也不通过 `RunOrchestrator` 生成固定 `fake-ok` output

### Requirement: Agent list API 和 CLI 使用同一 registry seam
系统 SHALL 提供 `agent-harness agents list` 和 `GET /api/v1/agents`，二者都通过同一 `AgentRegistry` 读取 descriptor，并使用稳定 DTO / `ApiErrorEnvelope`。

#### Scenario: CLI agents list 离线可用
- **WHEN** developer 在 local profile 下执行 `agent-harness agents list`
- **THEN** command 输出至少一个 registry smoke agent，且不需要真实模型 API key

#### Scenario: OpenAPI 包含 agents list 契约
- **WHEN** 生成 service-app OpenAPI schema
- **THEN** `/api/v1/agents` 存在 `GET` operation，成功响应使用 agent list schema，错误响应包含 `ApiErrorEnvelope`

#### Scenario: Registry validation error 映射到 API error envelope
- **WHEN** service app 启动或测试入口注入无效 registry
- **THEN** `/api/v1/agents` 返回稳定错误 envelope，而不是泄露 Pydantic、YAML 或本地路径异常

#### Scenario: Unknown agent run 被 registry 拦截
- **WHEN** 调用方通过 CLI 或 `POST /api/v1/agents/{agent_id}/runs` 请求不存在的 `agent_id`
- **THEN** 系统在进入 `RunOrchestrator` 前通过 `AgentRegistry` 拒绝请求，并返回 `registry.agent_not_found`

### Requirement: Delegation edge 与摘要接缝默认受控
系统 SHALL 从 agent descriptor 读取 delegation edge，并提供显式校验 seam；未声明 edge 时默认拒绝 agent 互调。声明 edge 只授予进入 delegation application service 的资格；service MUST 在创建 child run 前继续校验 tenant、identity、policy、cycle、depth、budget 与幂等请求，并按 parent 原子预留预算。成功执行后，系统 MUST 从已经通过非 bool、非负、有限数值与 `cost_status` 组合校验的持久化 child run/model/trace evidence 生成 parent/child 归属与聚合摘要；非法 evidence 必须进入 fail-closed/needs_review，不得信任调用方自报 usage、budget 或 trace refs，也不得让负值反向冲减 parent 预算。

#### Scenario: 未声明 delegation edge 被拒绝
- **WHEN** agent A 请求委派给 agent B 且 A 的 descriptor 未声明 B
- **THEN** 系统返回 `delegation.edge_denied`，只保留脱敏 policy/audit evidence，不创建 child run、queue message、provider call 或业务事件

#### Scenario: 已声明 edge 仍需完整授权
- **WHEN** agent A 请求委派给已声明的 agent B
- **THEN** 系统在创建 child run 前校验同租户 identity、policy、cycle/depth、幂等绑定，并让所有 key 按 parent 原子竞争可用预算，任一失败均 fail closed

#### Scenario: Cycle、depth 或 budget 超限被拒绝
- **WHEN** delegation 会形成 cycle、超过 P0 单层深度或有效预算不足
- **THEN** 系统分别返回 `delegation.cycle_detected`、`delegation.depth_exceeded` 或 `delegation.budget_exceeded`，且零 child/queue/provider/业务事件副作用

#### Scenario: Delegated usage 从可信 evidence 归并
- **WHEN** child run 达到 terminal 且 token、cost、latency 与 trace evidence 已持久化
- **THEN** parent run 可读取包含 child run、usage、budget impact 和 trace refs 的聚合摘要；input/output token 在混合已知/未知时为所有已知 child 值之和，全部未知时为 null，任一 child token 为 null 都令 `budget_status=incomplete`；`cost_usd` 仅在所有 child cost 可用时求和，任一 unavailable 时为 null 并令 `budget_status=incomplete`；`latency_ms` 仅在所有 child latency 可用时求和，任一未知时为 null 并令 `budget_status=incomplete`；三者都不得把未知值当 0，也不需要业务 agent 拼接 provider 原始事件

#### Scenario: 混合已知与未知 child evidence 不伪造零值
- **WHEN** 同一 parent 的 child evidence 同时包含已知 token/cost/latency 与 null/unavailable 值
- **THEN** token 只累计已知值，cost/latency 按缺失规则为 null，`budget_status=incomplete`，聚合保留逐 child evidence refs 供复核

#### Scenario: 全部 child token 未知时保持 null
- **WHEN** 同一 parent 的全部 child input token 或 output token evidence 都为 null
- **THEN** 对应聚合 token 字段为 null 且 `budget_status=incomplete`，不得用 0 伪造已知总量

#### Scenario: 不完整 evidence 不伪造聚合
- **WHEN** child terminal 存在但必需 usage 或 trace evidence 缺失
- **THEN** delegation 标记为 `needs_review` 或 incomplete，并保留已有 refs，不伪造 cost、budget 或成功聚合

#### Scenario: 非法 child usage 不能反向冲减预算
- **WHEN** child durable evidence 含 bool、负 token/cost/latency、NaN/Infinity 或不一致的 `cost_usd/cost_status`
- **THEN** aggregation 在求和与 reservation 结算前 fail closed并把 delegation 标记为 `needs_review`，parent 已用预算不减少、可用余额不增加，错误不回显 raw provider value

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

### Requirement: ContextAssembler 输出可解释 assembly trace
系统 SHALL 通过 `ContextAssembler` 收口 system/user/history/retrieval/tool output/artifact refs，按 token budget 执行裁剪和降级，并输出包含 source、trust_level、truncation 和 fallback decision 的 trace。每次 assembly SHALL 写入 `context_assemblies` 记录，包含 input refs、token budget、trust summary、truncation summary 和 output_ref。

#### Scenario: 多来源上下文被统一组装
- **WHEN** 历史、retrieval chunk、tool output 和 artifact ref 同时进入上下文
- **THEN** assembly 输出保留每个片段的 source_ref、trust_level、token estimate 和截断状态

#### Scenario: 超预算时按可解释顺序降级
- **WHEN** 输入上下文超过 token budget
- **THEN** assembler 先裁剪 history，再截断 retrieval/tool output，必要时记录 fallback model 或 policy-needed decision

#### Scenario: Context assembly 记录可持久化读取
- **WHEN** assembler 完成一次上下文组装
- **THEN** storage repository 可按 assembly id 读取 input refs、token budget、trust summary、truncation summary 和 output_ref

### Requirement: EmbeddingProvider 支持 mock/local、OpenAI-compatible adapter 和 cache
系统 SHALL 通过 `EmbeddingProvider` interface 生成 embedding，local tests 默认使用 mock/local provider，并通过 `tenant_embedding_cache` 持久化记录复用重复输入结果。cache key SHALL 包含 `tenant_id`、provider、model 和 input hash；所有 lookup、幂等复用、唯一性与 `vector_ref` MUST 按 tenant 隔离，不同 tenant 不得返回同一 cache record 或 `vector_ref`。cache metadata SHALL 持久化记录最近一次 hit/miss、稳定 `vector_ref` 和 provider latency 状态；新 provider 写入 MUST 使用 `provider_latency_status=recorded` 与非 bool 非负 `provider_latency_ms`，旧合同允许但无法确定 latency 的历史 row MUST 使用 `provider_latency_status=unavailable` 与 `provider_latency_ms=null`，不得猜测为 `0`。cache hit MUST 保留首次 provider latency 状态且不得伪造新的 provider 调用。新 schema MUST 不再暴露旧物理表名 `embedding_cache`，使忽略 tenant 的旧 binary 在查询时 fail closed。

#### Scenario: 同租户重复 embedding 输入命中 cache
- **WHEN** 同一 tenant、provider、model 和 input hash 第二次请求 embedding
- **THEN** cache 返回该 tenant 已有 vector ref 或 embedding result，把持久化 metadata 记录为 hit，并且不再次调用 provider

#### Scenario: 不同租户相同输入相互隔离
- **WHEN** tenant A 与 tenant B 使用相同 provider、model 和 input hash 请求 embedding
- **THEN** 两个 tenant 分别得到自己的 cache record 与不同 `vector_ref`，任一 tenant 都不能读取或复用另一 tenant 的记录

#### Scenario: Embedding cache 记录可跨 repository instance 复用
- **WHEN** 同一 tenant 在同一 SQLite 或 PostgreSQL storage 中重新构造 embedding cache repository
- **THEN** 第二次请求同一 provider、model 和 input hash 仍命中该 tenant 已有 cache record，持久化 metadata 保留 `vector_ref` 与首次 provider latency 状态；历史 unavailable 不得被改写为虚构数值

#### Scenario: OpenAI-compatible adapter 不污染业务边界
- **WHEN** 配置 OpenAI-compatible embedding provider
- **THEN** provider SDK / HTTP 细节只存在于 adapter 层，业务 agent 和 context assembler 只依赖 `EmbeddingProvider`

### Requirement: 路由和预算决策进入统一 usage evidence
`ModelRouter` SHALL 把实际选择的 provider/model、route/fallback 与 budget decision 传给受控 evidence seam；model/embedding adapter SHALL 返回可归一化的 usage 输入。业务 agent、template agent 和 API route MUST NOT 解析 provider raw event、导入 provider client 或手工填充 `ModelUsageEvidence`。

#### Scenario: Fallback decision 与实际调用一致
- **WHEN** router 因默认模型不可用或预算选择 fallback model
- **THEN** 调用级最终 usage evidence 同时记录原 route decision、实际 provider/model 和 budget impact，且字段来自 router/adapter 边界；该 evidence 不设置 run terminal marker

#### Scenario: 业务 agent 不拼接 raw usage
- **WHEN** import/static boundary 扫描业务 agent、template agent 和 API route
- **THEN** 这些表面不导入 provider usage object/client，也不创建或修改 `ModelUsageEvidence`

### Requirement: Agent budget 提供可信共享上界
Agent descriptor 的 `max_tokens_per_run` / `max_cost_usd_per_run` SHALL 表示 parent execution tree 共享硬上限。Token维度始终启用；cost维度仅在`max_cost_usd_per_run`非null时启用。Router/adapter MUST 为每个实际 model 或 embedding route 的每个已启用维度产生受信、有限的最坏 intent；cost关闭时不要求伪造cost上界。Fallback改变provider/model时 MUST在外部调用前按实际route重新校验并预约，调用方不得提供更小值绕过预算。

#### Scenario: Fallback 使用实际 route 上界
- **WHEN** router 从首选模型切换到 fallback provider/model
- **THEN** shared ledger 在调用 fallback 前使用该实际 route 各已启用维度的可信最坏上界，任一已启用维度无法证明上界时 fail closed；关闭的 cost 维度不因合法 unavailable 单独拒绝

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

### Requirement: 业务执行器必须通过可信绑定入口选择文本流
生产 Agent executor SHALL 只通过 `build_execution_context()` 注入的 `BoundModelInvocationService` 选择流式文本，不得直接取得未绑定的 `ModelInvocationService`，也不得从业务输入接收 tenant、run、agent、trace 或 `usage_call_id`。绑定 façade MUST 同时提供异步 `stream(request, operation_key=...) -> ModelResponse` 与 `stream_approved(request, operation_key=..., grant=...) -> ModelResponse`：普通入口以受信运行上下文和调用方提供的语义 `operation_key` 生成稳定 `usage_call_id`；审批入口 MUST 复用既有 grant 全绑定、单次 lease 与 current hard-gate 重检，并把唯一 identity 固定到 `approved:{grant.approval_id}`，不得通过改变 `operation_key` 扩成第二次 provider 调用。返回值只在 durable completed/usage 闭合后给出最终 `ModelResponse`，增量不从该返回值或 iterator 暴露；SSE/CLI 仍只读取 committed events。

#### Scenario: 运行上下文暴露可信普通流式入口
- **WHEN** runtime 以 `build_execution_context()` 绑定 model invocation service，业务 executor 从 context 取得该服务并调用 `stream(request, operation_key="answer")`
- **THEN** façade 使用可信 tenant、run、agent、request、trace 与语义槽位生成稳定 `usage_call_id`
- **AND** 业务 executor 无法覆盖上述身份，也无法取得底层未绑定 stream seam

#### Scenario: 审批续跑只能消费唯一流式调用槽位
- **WHEN** soft policy 要求审批且 continuation 携带匹配的 durable approval grant
- **THEN** `stream_approved` 复用既有审批绑定、单次 lease 与当前 hard-gate 重检
- **AND** `usage_call_id` 的语义槽位固定使用 `approved:{approval_id}`，调用方传入的 `operation_key` 只用于可读关联，不能制造额外 provider 调用

#### Scenario: 未批准或不匹配的流式审批零副作用
- **WHEN** 普通 `stream` 命中 `require_approval`，或 `stream_approved` 收到缺失、过期、已消费或字段不匹配的 grant
- **THEN** 调用在 stream/usage 容量、started、client send 与 provider 迭代前以既有 policy/approval 稳定错误停止
- **AND** 不发布 delta/completed，不允许调用方绕到底层 stream seam

### Requirement: 路由按供应商中立能力协商文本流
模型 route SHALL 使用受信任 capability `text_stream` 显式声明增量文本能力。router MUST 通过独立的 `prepare_stream` seam 取得 `PreparedModelStreamCall`，并保持 prepare 阶段无网络副作用；只有 invocation 在容量、预算、outbox 和 started 证据均成功后调用 send/iterate，才允许第一次供应商副作用。既有 `text_completion` 与 `complete` 行为不得改变。

#### Scenario: 流式 prepare 不产生网络副作用
- **WHEN** invocation 为支持 `text_stream` 的 route 调用 `prepare_stream`
- **THEN** router 可以取得并持有 provider permit/client lease，但不发送网络请求、不消费响应流
- **AND** invocation 完成全部前置持久化后才开始迭代 provider stream

#### Scenario: 一次性调用保持兼容
- **WHEN** 调用使用既有 `text_completion` capability
- **THEN** router 继续使用既有 `prepare`/`complete` 协议
- **AND** 新流式协议不会改变 fake、测试 double 或一次性 Pydantic AI 调用结果

### Requirement: 流式 provider 关闭结果必须可分类
`PreparedModelStreamCall` SHALL 提供确定性的本地资源关闭，并返回 provider-neutral `ModelStreamCloseResult`。结果 exact shape 为 `state=not_started|stopped|unknown` 与 nullable `ModelStreamUsage`；usage 包含 `finality=partial|complete`、nullable token/cost、受校验 cost status 与非负 latency，不得含 SDK 类型。`not_started` 禁止 usage；`stopped` 只有在适配器能够证明远端不会继续产生副作用时才允许，且可携带 partial/complete usage；`unknown` 只允许 null/partial usage。调用方取得 iterator 不等于 provider 已开始；若 deadline 在 SDK stream context 创建前耗尽，adapter MUST 仍返回 `not_started`。一旦 context 已创建，普通 context 退出、task cancellation、socket 关闭或本地超时本身不得被当作停止证明。适配器 MUST 在退出时清理本地后台任务和 client lease，但不得因此伪造远端已停止。

#### Scenario: 未开始即关闭
- **WHEN** 调用方已请求首次迭代，但 deadline 在 SDK stream context 创建前耗尽，或 provider stream 尚未开始迭代就被关闭
- **THEN** seam 返回 `not_started` 并释放本地资源
- **AND** 若双预留事务尚未提交则随 UoW 回滚；若 durable started 已发布，则系统保留 started、取消 stream 占位并通过 not-started cancelled usage final 闭合容量和预算，不撤销已持久化 evidence

#### Scenario: 本地取消无法证明远端停止
- **WHEN** 已开始的 provider stream 因 task cancellation 或连接异常退出，且供应商没有停止确认
- **THEN** seam 返回 `unknown`
- **AND** invocation 保留未决结算与终态围栏

#### Scenario: 已证明停止并返回完整 usage
- **WHEN** provider 明确证明远端停止且返回完整、可信的 input/output 与当前启用 cost 维度
- **THEN** seam 返回 `state=stopped`、`usage.finality=complete` 的 provider-neutral close result
- **AND** invocation 可从该 DTO 生成中断 usage evidence，不读取 SDK object

#### Scenario: unknown 仅携带已观察 usage
- **WHEN** adapter 已观察部分 token/cost 但无法证明远端停止
- **THEN** seam 只可返回 `state=unknown`、`usage.finality=partial`
- **AND** 该 usage 只进入 attempt 审计，不授权结算、退款、lease 释放或 terminal

### Requirement: Pydantic AI 锁定版本使用原始事件流
Pydantic AI adapter SHALL 使用项目锁定版本的 `Agent.run_stream_events` 原始事件流，并消费到唯一最终结果事件。适配器 MUST 只把 `TextPart` 的 start/delta 追加内容转为文本增量；tool、reasoning、structured 和其他事件保持私有。适配器 MUST 验证最终事件存在且只出现一次，并从最终 `AgentRunResult` 提取输出与 provider usage。SDK usage 在一次 adapter 生命周期内 MUST 最多读取一次并缓存 provider-neutral 转换结果；读取抛异常，或 bool、负数、非整数等值无法通过公共 usage 合同时，调用结果与关闭结果 MUST 稳定归类为 `unknown`，本地 `aclose()` 不得再次读取同一 SDK usage 或把该异常逃逸到 invocation 之外。不得使用跳过结果校验的捷径，也不得依赖供应商原生 cursor 作为恢复身份。

#### Scenario: 原始事件流正常结束
- **WHEN** 锁定 Pydantic AI 返回若干文本 part 事件并以一个 `AgentRunResultEvent` 结束
- **THEN** 适配器按追加顺序输出文本片段，并产生一个包含最终输出和 usage 的 `ModelResponse`
- **AND** 调用完成前事件流被消费到最终结果

#### Scenario: 最终结果事件缺失或重复
- **WHEN** 原始事件流结束时没有最终结果，或出现多个最终结果
- **THEN** 适配器关闭失败并将副作用状态按可证明事实分类
- **AND** 不合成最终响应、不发布 completed 或零 usage

#### Scenario: SDK usage 无法安全读取或转换
- **WHEN** 唯一最终事件存在，但 SDK usage accessor 抛异常，或返回 bool、负数、非整数等非法值
- **THEN** adapter 对 result 与 close seam 复用同一次读取事实，返回稳定 `model.provider_side_effect_unknown` 与 `state=unknown`
- **AND** `aclose()` 不再次读取 SDK usage、不抛出原始异常，invocation 将 usage、共享预算与 owner ledger 耐久提升为 needs-review
