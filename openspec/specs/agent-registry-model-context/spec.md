# agent-registry-model-context Specification

## Purpose
定义 Agent Registry、模型路由、ContextAssembler 与 Embedding provider/cache 的长期契约，使多 agent 发现、provider 边界、上下文组装和 embedding 复用有稳定 public seam。
## Requirements
### Requirement: AgentRegistry 加载并校验多个 agent descriptor
系统 SHALL 从受控 agent config 目录加载多个 `AgentDescriptor`，并拒绝重复 `agent_id`、无效 schema 或缺少必要字段的配置。每个 `config.yaml` MUST 声明 `agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model.provider`、`model.deployment_id`、`model.allowed_models`、`model.default_model`、`model.fallback_models`、`budget.max_tokens_per_run`、`budget.max_cost_usd_per_run`、`tool_allowlist`、`eval_dataset` 和 `delegation_edges`；default/fallback models MUST 是 allowed models 的子集且不得重复。缺少 `model.fallback_routes` 时保持 legacy 单 deployment 模式，上述 model 字段的既有含义和校验不变。显式提供 `fallback_routes` 时进入 route-chain 模式：它是跨 deployment 授权与顺序的唯一真相源；既有字段只作为首候选的确定性兼容投影，`provider` MUST 等于首候选 deployment 的 `provider_kind`，`deployment_id`/`default_model` MUST 等于首个 ref，`allowed_models` MUST 按 route 顺序去重投影该首 deployment 中已列出的 models，`fallback_models` MUST 为空。兼容投影不得授权未列入 `fallback_routes` 的 route，也不得让后继候选继承首 deployment 的 provider、catalog、credential、capability 或预算。

Registry MUST 在同一次全量加载中把每个 Agent 的 `output_schema` 解析为严格 canonical JSON Schema 与 `output-schema-identity-v1`，并在 descriptor、executor 与全部 schema 都验证成功后原子替换只读 catalog；任一 sibling schema 无效时 MUST 整体拒绝，不能留下部分可运行 catalog。public descriptor SHALL 只暴露 `agent_id`、`version`、`name`、`description`、输入/输出 schema refs、与输出 ref 匹配的 provider-neutral `output_schema_identity`、相对 `config_ref`、tool policy summary、model policy summary、budget summary、eval dataset ref 和 delegation target ids；`output_schema_identity` exact fields 为 `schema_version="output-schema-identity-v1"`、`schema_ref`、descriptor `version` 和严格 canonical JSON Schema 的 64 位小写 SHA-256 `digest`。chain summary SHALL 保留有序 `(deployment_id, model_id)` refs 与上述投影，但 public descriptor MUST NOT 暴露本地绝对路径、provider secret、endpoint、callable、catalog price、provider client、Python class、module object、Pydantic AI 或 provider SDK 类型。

既有 `examples.dev_assistant` 的 `DevAssistantOutput.result` 不得继续使用会生成 `additionalProperties=true` 的 `dict[str, object]`。只有核心schema compiler、catalog与公开structured seam先稳定后，Phase 19才可把它迁移为严格 `DevAssistantToolResult`：仅允许当前read/write/shell完成结果的`path/content/bytes/artifact_ref/exit_code/stdout/stderr/stdout_ref/stderr_ref/duration_ms`字段，字段按实际工具结果保持可选或nullable，未知工具结果字段关闭失败；外层既有status、tool_name、source/artifact/policy/trace引用语义保持不变。该示例迁移只适配Registry严格加载，不定义核心structured DTO，不增加工具执行路径，也不得放宽全量原子失败规则。

既有 `examples.rag_assistant` 的 `RagOutput.assembly_truncation` 不得继续使用会生成schema-valued `additionalProperties` 的 `dict[str, int]`。公开structured seam稳定后，Phase 19 SHALL 将该字段迁移为两个互斥exact object的封闭union，两者都递归`additionalProperties=false`：`RagAssemblyTruncationEmpty`不含任何字段，canonical payload固定为`{}`；`RagAssemblyTruncation`的exact字段只允许`input_count/retained_count/truncated_count/dropped_count/used_tokens/fragment_count`，每项都是必填、非bool、非负整数。`status=no_source`当且仅当使用empty变体，并要求`assembly_id/model_provider=null`、citations/source refs为空；该变体只由检索结果为空且未创建Context Assembly的本地分支构造，不伪造六个零计数或assembly耐久记录。`status=completed`当且仅当使用六字段变体，并要求非空`assembly_id/model_provider`；Executor只从`ContextAssemblyResult.truncation_summary`的同名六个已冻结producer字段构造它。任何部分六字段、empty与completed、六字段与no-source或其他混搭都关闭失败。外层`assembly_truncation`字段名、citation/trust/assembly/model/trace语义和既有RAG离线流程保持不变。该兼容迁移不把RAG示例schema当作SDK核心类型，不改写Context Assembly的耐久字典schema，也不放宽严格compiler。

#### Scenario: 列出已配置 agent
- **WHEN** 调用方通过 CLI 或 API 请求 agent 列表
- **THEN** 系统返回已配置 agent 的 public descriptor 字段、输出 schema identity 和不含 endpoint/credential 的 deployment/model policy summary；chain mode 还按原顺序返回 route refs，且不暴露本地路径、provider secret 或内部对象

#### Scenario: Descriptor 字段契约完整
- **WHEN** registry 加载 smoke agent config
- **THEN** descriptor 包含 `agent_id`、`version`、输入/输出 schema refs、与输出 ref/version/canonical definition 逐值一致的 `output_schema_identity`、相对 `config_ref`、deployment/model 策略、预算、工具白名单摘要、eval dataset 和 delegation edge 列表；显式 chain 的 legacy 投影逐值匹配首候选

#### Scenario: 重复 agent_id 被拒绝
- **WHEN** registry 加载到两个相同 `agent_id` 的配置
- **THEN** registry 失败并返回稳定错误码，错误详情包含冲突的 `agent_id`

#### Scenario: 无效 agent config 被拒绝
- **WHEN** agent config 缺少必要字段、字段类型不合法，legacy default/fallback models 扩大或脱离 allowed models，chain 的兼容投影、route ref 与 typed deployment 不一致，或 schema reference 逃逸目录、目标缺失、canonical 化失败、允许额外字段
- **THEN** registry 失败并返回 registry validation error，不创建部分可用的脏 registry，也不导入 executor、构造 client或访问网络

#### Scenario: Scaffold 生成严格且可离线运行的 fake Agent 配置
- **WHEN** 调用方执行 `agent-harness scaffold agent` 生成新 Agent package
- **THEN** 生成的 `config.yaml` 显式写入 `model.provider=fake`、`model.deployment_id=fake_default`、`model.allowed_models=[fake-scaffold]`、`model.default_model=fake-scaffold` 与空 fallback，且不写 `fallback_routes`；local/service profiles 的 `fake_default` deployment 允许该模型，生成包通过正式 Registry、schema catalog 校验和离线 runtime 执行，且不读取真实 credential 或访问网络

#### Scenario: Chain 兼容字段不能扩权
- **WHEN** `fallback_routes` 为不同 deployment 的 A、B、C，而 legacy `provider/deployment_id/allowed_models/default_model/fallback_models` 不是首 deployment 的规定投影，或试图借 `allowed_models/fallback_models` 加入未列 route
- **THEN** registry 在 executor import、client 构造和网络前关闭失败；合法投影只供旧 reader 摘要使用，Router 仍只接受 A、B、C

#### Scenario: Registry 原子加载 schema 与 executor
- **WHEN** 目录中所有 Agent descriptor、schema 与 executor 均有效
- **THEN** 每个 Agent SHALL 同时可解析 descriptor、executor 与匹配的 output schema identity/definition，排序和重载保持稳定；内置`examples.dev_assistant` SHALL 以严格工具结果DTO通过同一catalog，且read/write/shell既有输出引用与离线流程不退化；`examples.rag_assistant` SHALL 以`no_source + {}`或`completed + 严格六字段组裁摘要`中唯一匹配的封闭union通过catalog，并保持citation/trust/assembly/model/trace与实际组裁计数逐值不变

#### Scenario: 任一 schema 无效则整体失败
- **WHEN** 一个 sibling 的 schema reference 逃逸目录、目标缺失、不是受支持 schema、canonical 化失败或允许额外字段
- **THEN** Registry SHALL 以稳定 `RegistryLoadError` 整体拒绝，其他 Agent 不得以部分 catalog 继续运行

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
系统 SHALL 通过 `ModelProvider` interface 和 `ModelRouter` 选择 deployment/default/任务级模型，执行 timeout、有限 fallback 和预算估算，并为 deployment、provider、budget 配置变更提供显式 restart/reload seam。`provider_kind` 是 deployment 的协议/构造族，`pydantic-ai` 仍只是私有 adapter 实现名。Legacy 单 route 中，`AgentModelPolicy.provider`、可选 `ModelRequest.provider` assertion、`ModelRoutePlan.provider`、公开 evidence `provider` 与绑定 `ModelProvider.provider_id` MUST 逐字等于唯一 deployment 的 `provider_kind`，现有 deployment∩Agent∩request 交集、同 deployment fallback 和 fake 兼容行为不变。显式 route chain 中，Agent 的单值 provider/deployment/model字段只断言请求缩权前原始 `fallback_routes[0]`投影，不得套用到后继或随request改写；Router MUST 对每个 `ModelRouteRef` 独立验证 `candidate.provider == referenced deployment.provider_kind == bound ModelProvider.provider_id`、model 属于该 deployment catalog/allowlist，且只允许 `fake|openai-compatible`。`ModelRequest.route_refs` 只能选择 Agent chain 的非空有序子序列；请求只携带单值 `provider/deployment_id/model` 时等价缩权到唯一匹配 ref，若同时携带 route refs 则该子序列 MUST 只有一个且逐值一致。请求不得借首候选兼容投影授权、插入或重排后继候选。

Router MUST 先计算每个 deployment allowed models 与对应 Agent route ref 的交集，再让 request 仅作进一步缩权；settings 只要含非 fake 受控 deployment，公共 `plan()`/`route()` 缺少 Agent model policy 就 MUST 返回 `model.route_not_allowed`，MUST NOT 回退 legacy/fake 路由。request 中兼容保留的 provider 字段只能作为上述相等断言，MUST NOT 选择 provider、endpoint 或 credential。`ModelRequest` MUST `extra=forbid` 且不声明 endpoint/credential 字段。Router MUST 在 soft policy/fallback/approval、预算预约与 provider 副作用前产出 immutable legacy `ModelRoutePlan` 或 `ModelRouteChainPlan`；chain plan 按冻结 ordinal 为每个候选分别保存 deployment/provider/model、canonical base URL/origin、endpoint policy/default catalog ref/version/digest、model catalog ref/version/digest、request-shape/strategy/envelope/price、completion classifier ref/version、credential ref identity、capability、prompt UTF-8 bytes、trusted input bound、output cap、每 attempt token/cost 上界、`max_attempts`、候选 reservation 上界、timeout/retry/Bulkhead 与 snapshot schema version，并冻结完整 chain digest/count。decision、retry、Bulkhead 与 candidates 也 MUST 是不可变 typed DTO，禁止外层 frozen 而内层可变。每个实际进入的候选在动态 hard eligibility 后独立执行 soft policy/fallback/approval；deny 或 approval-required 未获批准时该候选的 reservation/permit/client/mark/network 全部为零，任何 approval 都不得提高、重置或覆盖 shared hard limit。

#### Scenario: Fake model 不需要真实 API key
- **WHEN** local profile 使用 fake deployment 运行 tests 或 smoke
- **THEN** 模型调用成功返回可预测结果，且不读取真实 provider key、不创建真实 provider client或访问网络

#### Scenario: 三层模型范围只取交集
- **WHEN** legacy deployment、Agent descriptor 与 request 声明不同模型范围
- **THEN** route plan 只选择 deployment allowlist ∩ Agent allowed models 中由 request 选定或 Agent default/fallback 决定的模型，且调用后修改原 settings、descriptor、request 或 mapping 都不能改变该 plan

#### Scenario: 受控路由缺少 Agent policy
- **WHEN** Router settings 已包含非 fake 受控 deployment，但调用方通过公共 `plan()`、`plan_chain()` 或 `route()` 没有提供 Agent model policy
- **THEN** Router 返回 `model.route_not_allowed`，provider call count 为零，不得按 request/config 回退 legacy/fake 路由；完全没有受控 settings 的既有 fake 兼容入口保持离线可用

#### Scenario: 请求尝试扩大路由范围
- **WHEN** request 选择未知 deployment/model、provider 断言不匹配、插入或重排 Agent chain，或借 legacy 投影选择未列 route
- **THEN** router 返回 `model.request_invalid` 或 `model.route_not_allowed`，在预算预约、授权成功 evidence 与 provider call 前失败；允许的本地拒绝 evidence 必须脱敏且不得声称 route 已授权

#### Scenario: Provider assertion 与绑定 adapter 使用同一身份
- **WHEN** 任一候选的 request assertion、referenced deployment、冻结 plan 或绑定 adapter provider identity 不一致
- **THEN** registry/composition 或 router 在预算预约、client 构造和 provider call 前返回 `config.invalid` 或 `model.route_not_allowed`；一致的 `openai-compatible` route 不得因 adapter 使用 Pydantic AI 实现而改写成 `pydantic-ai`，后继候选也不得继承首候选 provider

#### Scenario: Prompt、输出 cap 与价格公式在副作用前冻结
- **WHEN** 任一真实候选的 prompt、output cap、catalog strategy、token/cost bound、价格公式或来源不合法
- **THEN** router 在该候选 reservation、Bulkhead、client lease 获取/构造和 provider call 前将其静态关闭或以 `budget.reservation_rejected` 终止；不得用其他候选价格补齐，合法候选只传递自己的冻结 output cap

#### Scenario: 请求 capability 不受支持
- **WHEN** request 请求 legacy route 或 chain candidate 未声明的 capability
- **THEN** router 返回 `model.capability_unsupported` 或把该候选记录为静态不合格，在预算预约和 provider call 前失败；不得跨候选继承 capability

#### Scenario: Soft policy 与 approval 早于 reservation
- **WHEN** 某候选 hard eligibility 已通过，但其 soft policy deny、触发 approval-required 或尚未取得批准
- **THEN** runtime 对该候选 exact route/catalog/bound context 执行 PolicyEngine；policy audit 可持久化，但该候选不建立 reservation、不取得 permit、不获取或构造 client、不写 durable mark且不发网络；前一候选的结论不能授权它

#### Scenario: Endpoint 或 credential override 在 DTO 边界被拒绝
- **WHEN** 原始请求携带 `endpoint`、`base_url`、`credential`、`credential_ref` 或其他未声明字段
- **THEN** request validation 返回 `model.request_invalid`，router 不执行且没有预算、model policy audit 或 provider 副作用

#### Scenario: 预算超阈值产生可追踪 fallback decision
- **WHEN** legacy 模型或 chain candidate 的预计 token/cost 超过配置阈值
- **THEN** `ModelRouter` 只从各自冻结授权范围内形成可追踪 decision；chain 可在零 provider 副作用下继续检查下一候选，但每个候选重新经过 hard eligibility、独立 policy 与预算判断

#### Scenario: Cost hard limit 缺少可信价格
- **WHEN** cost hard limit 启用但任一实际候选缺少 input/output price、catalog ref 或 version
- **THEN** 该候选在 provider 副作用前以静态不合格或 `budget.reservation_rejected` 收敛，cost 不被当作 0，也不自动切换 fake

#### Scenario: 业务 agent 不直接 import Pydantic AI
- **WHEN** import boundary check 扫描 runtime core、template app 和业务 agent
- **THEN** `pydantic_ai`、`openai` 和 SDK client 类型只允许出现在批准 vendor boundary 或测试替身中；route chain 不增加新的 SDK 泄漏豁免

#### Scenario: 两个 deployment/provider 候选逐项冻结
- **WHEN** Agent chain 为两个不同 deployment/provider refs，request 保留原顺序并删减为其非空子序列
- **THEN** Router 按该子序列冻结两个相互隔离的 candidate plans、各自 provider/catalog/credential identity、价格和 Bulkhead；首候选 legacy 投影不覆盖第二候选

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
模型调用 SHALL 在 hard eligibility 后、reservation前通过既有 `PolicyEngine.evaluate(PolicyCheck)`：actor MUST是 runtime绑定的 `IdentityContext`，action MUST为 `model.invoke`，resource MUST为 `agent:<agent_id>:model`，context MUST只包含 tenant/run/agent/request/trace、当前候选冻结 route/model-catalog identity、reservation bounds和 soft-limit decision，不含 prompt、secret或完整 URL。每个 route-chain candidate必须独立执行该检查；前一候选 allow/approval不能授权后继。`PolicyEngine`的既有 `AuditService` `policy.decision`记录及 `audit_ref` SHALL是唯一授权成功审计；MUST NOT新增 model authorization CanonicalEvent，也不得占用 model event capacity或声称 provider已开始。

`require_approval` SHALL复用既有 `AgentApprovalRequest`、`policy_approval` checkpoint、`ApprovalRecord`/resolution lease、`ApprovalGrant`、`ApprovalService`与 `RunOrchestrator.resume_run()`。Legacy单 route等待与续跑语义不变：`BoundModelInvocationService.complete_approved()`只接受已验证 grant，稳定 operation identity继续绑定 approval id，current balance不足仍按既有 hard reject。显式 route chain则 MUST在首次可信 bound entry、任何 policy/approval record前以原始语义 operation key生成 `usage_call_id`与 `operation_identity_digest`；等待状态、零 impact coordination claim、checkpoint、ApprovalRecord/Grant必须冻结并绑定这两个字段、action/resource、canonical `ModelRequest` arguments hash、tenant/identity/agent/run/request/trace。ApprovalService只从 durable record/lease构造 grant；runtime在调用 approved seam前从私有 checkpoint重算初始 identity，并逐值校验 waiting state、approval id、lease、tenant、identity、agent、run、action、resource、arguments hash、状态与单次 lease。Chain `complete_approved()` MUST复用原 claim/settlement/outbox并重新执行目标候选 hard route/catalog/current owner balance，不得用 `approved:<approval_id>` rekey、建立映射或创建第二 claim；目标候选 current balance不足时以 `budget_ineligible/balance`零 impact收敛，只有更后 ordinal重新执行自己的 policy后才可继续，grant不得跨候选复用。两种模式下 approval都只能继续原 intent或进一步缩权，不能提高、重置或覆盖 hard limit；mismatch、stale、duplicate/replay均 fail closed，获批 continuation最多触发一次 provider调用，crash/replay只恢复同一 settlement。证据继续复用既有 `policy.decision` audit、`approval.required|resolved`与 `run.resumed`。

#### Scenario: Policy allow audit 不伪装 provider side effect
- **WHEN** exact route-chain candidate Model PolicyCheck返回 allow，runtime建立reservation和该global attempt的durable started identity后，permit或client factory在send前失败
- **THEN** audit保留decision=allow与安全关联字段；runtime以同一UoW将该attempt关闭为`not_started_proven/client_not_started`并写proof，按冻结retry/transfer规则处理reservation
- **AND** 不新增授权CanonicalEvent、不声称远端provider已开始、不发网络，也不删除或回退已提交的attempt identity；legacy单route的factory-before-mark行为保持不变

#### Scenario: Require approval 创建 durable waiting 状态
- **WHEN** legacy模型或 route-chain任一候选 policy返回 require_approval
- **THEN** runtime创建绑定 exact request hash的既有 approval/checkpoint并停止，目标候选 reservation/permit/client/mark/network为零；chain另以 approval前冻结的 usage identity建立或保留零 impact coordination row
- **AND** deny只保留 policy audit，不把 grant或前一候选结论复制到后继

#### Scenario: Legacy bound grant 续接恰好一次
- **WHEN** legacy单 route ApprovalService从已批准 durable lease构造全绑定 grant并恢复相同 checkpoint
- **THEN** runtime校验全部绑定与单次 lease，以 approval id绑定既有 operation identity，重算 hard route/catalog/current balance后最多执行一次 provider调用；相同 crash/replay只恢复同一 settlement

#### Scenario: Chain bound grant 复用 approval 前调用身份
- **WHEN** route-chain ApprovalService产生匹配 grant并恢复相同 checkpoint
- **THEN** runtime重算并匹配首次 usage call id/operation digest，shared-budget以同一 identity/request/grant digest激活原 claim，最多执行目标 provider一次
- **AND** 不生成 `approved:<approval_id>`派生 ID，不 rekey、不创建第二 settlement/outbox/stream group

#### Scenario: Chain 获批候选余额不足后重新授权后继
- **WHEN** chain候选 B获得合法 grant但 activation时 current balance不足，且还有候选 C
- **THEN** B耐久成为 `budget_ineligible/balance`且零 provider；C重新执行独立 Policy/HITL
- **AND** B的 approval/grant不能授权 C，恢复不能因新余额重新激活 B

#### Scenario: Mismatch、stale、伪造身份或重复 grant fail closed
- **WHEN** grant的 approval/lease/tenant/identity/agent/run/action/resource/hash、chain usage identity任一不匹配，状态陈旧、lease已消费，或 continuation试图 rekey并再次开始 provider
- **THEN** continuation在 reservation/client/network前拒绝，不允许调用方以 bool、原始 token或新 operation key绕过 soft gate，也不提高 shared hard limit

### Requirement: Runtime composition 通过 lazy client factory 注册受控非流式 provider
Composition root SHALL 从已验证 settings 和 registry 构造 provider-neutral router：`fake` deployment 注册离线 provider；真实 deployment通过唯一批准的 Pydantic AI adapter 注入已验证 typed endpoint/model catalogs、credential/base URL/deadline/retry/bulkhead/completion-classifier 的私有 lazy `ControlledOpenAIClientFactory` blueprint，而不是在 startup 预构造 async client。Legacy单route继续严格按 immutable route执行 `hard eligibility → soft policy/audit → reservation → Bulkhead permit → client lease → durable side_effect_started mark → send`；factory失败仍按既有规则在mark前回滚。显式route chain唯一顺序改为 `hard eligibility → candidate policy/audit → candidate reservation → durable attempt_lifecycle started identity → Bulkhead permit → candidate-isolated client lease → send/iterate`：attempt identity必须绑定chain、usage/operation、candidate/global attempt与route/endpoint/retry digest，并在任何permit/client/send副作用前提交。Permit拒绝、factory获取/构造失败或adapter在send前确定失败时，runtime MUST在owner shared-budget UoW把同一attempt从`started`原子关闭为`not_started_proven`、追加`client_not_started` proof并按冻结retry/transfer/terminal规则处理reservation；不得删除started record或把它回滚成不存在。若started提交确认未知，或进程在started提交后、关闭提交前崩溃，恢复必须保留reservation/needs-review，不得重取permit/client、重发该attempt、创建下一attempt或推进provider。

SDK client构造不得联网。Provider/factory/已构造client lease生命周期 MUST由API、worker和CLI composition统一幂等关闭；SDK Agent/result/client不得进入业务Agent、checkpoint、descriptor或public DTO。锁定SDK的ambient env/header/origin隔离、typed credential、`trust_env=False`、`follow_redirects=False`与socket send前origin/base-path/header allowlist校验继续逐deployment执行；一个candidate的permit/client/credential/endpoint/catalog不得复用于另一个candidate。

#### Scenario: 有效 deployment 构造真实 provider
- **WHEN** startup取得完整真实deployment、匹配Agent policy与已解析typed credential
- **THEN** composition只注册按`deployment_id`寻址的lazy provider/factory，client-construction count为零
- **AND** legacy按既有permit→client→mark→send执行；route chain在candidate reservation后先提交attempt started identity，再取得该candidate permit/client并最多send一次

#### Scenario: 启动失败没有部分 runtime
- **WHEN** 任一真实 deployment 的 endpoint、credential、capability、价格或 policy 无效
- **THEN** API、worker 与 CLI composition 在接受 run、创建attempt identity、连接 provider 或发布业务 evidence 前结构化失败，不留下只注册部分 provider 的 runtime

#### Scenario: OpenAI SDK ambient env 不能改变受控请求
- **WHEN** 进程注入ambient key/base URL/org/project/webhook/custom header，而合法route使用另一组typed credential与冻结endpoint
- **THEN** client/transport只观察冻结typed plan/allowlist，ambient key/header/origin均未出站，进程全局环境未修改
- **AND** 每个route-chain candidate使用自己的deployment identity，不共享ambient或前一candidate client

#### Scenario: 动态拒绝不触发 attempt 或 lazy client
- **WHEN** deployment静态配置有效，但request的prompt/output/strategy/price/formula不满足dynamic hard eligibility，或policy deny/require-approval未获批准
- **THEN** runtime在candidate reservation、attempt started identity、permit、factory和network前停止；已有其他route cached client不得被获取

#### Scenario: Client factory 失败保留 route-chain attempt identity
- **WHEN** 合法route-chain candidate已取得reservation并提交attempt started identity，但permit或factory获取/构造lease失败
- **THEN** runtime不发网络，并在同一owner UoW写`client_not_started` proof、关闭为`not_started_proven`，再按冻结policy原子retry、transfer或terminal
- **AND** 关闭提交失败或确认未知时保留reservation/needs-review；不得删除mark、再次取得client、重发该attempt或伪造零调用terminal

#### Scenario: Composition 关闭网络资源
- **WHEN** API lifespan、worker或CLI runtime退出或启动中途失败
- **THEN** 所有已创建async provider client幂等关闭，再完成其余资源清理；关闭动作不改写attempt lifecycle、不泄漏连接或secret

### Requirement: 业务执行器必须通过可信绑定入口选择文本流
生产 Agent executor SHALL 只通过 `build_execution_context()` 注入的 `BoundModelInvocationService` 选择流式文本，不得直接取得未绑定的 `ModelInvocationService`，也不得从业务输入接收 tenant、run、agent、trace 或 `usage_call_id`。绑定 façade MUST 同时提供异步 `stream(request, operation_key=...) -> ModelResponse` 与 `stream_approved(request, operation_key=..., grant=...) -> ModelResponse`。Legacy 单 route 保持 Phase 18.1 行为：普通入口以受信运行上下文和语义 `operation_key` 生成稳定 `usage_call_id`；审批入口复用既有 grant 全绑定、单次 lease与 current hard-gate 重检，并把身份语义槽位固定到 `approved:{grant.approval_id}`。显式 route chain 则 MUST 在首次可信入口、任何 policy/coordination row/approval record 前，以受信上下文和调用方原始语义 `operation_key` 生成唯一 64 位小写 SHA-256 `usage_call_id` 与 `operation_identity_digest`；waiting coordination state、私有 checkpoint、approval request/record/grant 和后续 reservation/settlement/outbox MUST 绑定并复用这一身份。Chain 的 `complete_approved()`/`stream_approved()` MUST 从受信 checkpoint 取回原始 operation identity并重算，逐值匹配 waiting state、approval metadata 和 grant；不得改用 `approved:{approval_id}` 生成新 ID，不得 rekey、映射或新建第二 claim。调用方传入的 operation key不能覆盖 checkpoint；不匹配、缺失、过期或伪造的 ID/digest 必须在 reservation、stream/usage capacity、client 和 provider 前关闭失败。返回值只在 durable completed/usage 闭合后给出最终 `ModelResponse`，SSE/CLI 仍只读取 committed events。

Chain `usage_call_id` SHALL 精确复用既有 `stable_usage_call_id(context, operation_key)`；`operation_identity_digest` SHALL 对 `model-route-chain-operation-v1`、tenant id、run id、agent id、request id或空串、trace id、原始 operation key按此顺序以 `U+001F`连接后取 SHA-256。两者都是 64位小写十六进制，原始 key只进入受信 checkpoint，不能由业务请求或 approval覆盖。

#### Scenario: 运行上下文暴露可信普通流式入口
- **WHEN** runtime 以 `build_execution_context()` 绑定 model invocation service，业务 executor 从 context 取得该服务并调用 `stream(request, operation_key="answer")`
- **THEN** façade 使用可信 tenant、run、agent、request、trace 与语义槽位生成稳定 `usage_call_id`；chain mode 同时在 approval 前冻结 operation identity digest
- **AND** 业务 executor 无法覆盖上述身份，也无法取得底层未绑定 stream seam

#### Scenario: Legacy 审批续跑只能消费唯一流式调用槽位
- **WHEN** legacy 单 route soft policy 要求审批且 continuation 携带匹配的 durable approval grant
- **THEN** `stream_approved` 复用既有审批绑定、单次 lease 与当前 hard-gate 重检
- **AND** `usage_call_id` 的语义槽位继续使用 `approved:{approval_id}`，调用方传入的 `operation_key` 只用于可读关联，不能制造额外 provider 调用

#### Scenario: Chain 审批前后复用原调用身份
- **WHEN** route chain 在任一候选 policy 处进入 waiting，随后 matching approval/grant 恢复同一 continuation
- **THEN** waiting row、approval record/grant、activation、stream group、usage settlement 与 outbox 逐值复用首次入口生成的同一 `usage_call_id` 和 operation identity digest
- **AND** resume 从私有 checkpoint 的原始 operation key重算一致；不得使用 `approved:{approval_id}` 重算、rekey 或创建第二 claim/provider 调用

#### Scenario: 未批准、身份不匹配或伪造调用 ID 零副作用
- **WHEN** 普通 `stream` 命中 `require_approval`，或 `stream_approved` 收到缺失、过期、已消费、字段不匹配的 grant，或 chain checkpoint/state/grant 中的 usage identity不一致
- **THEN** 调用在 reservation、stream/usage 容量、started、client send 与 provider 迭代前以既有 policy/approval 稳定错误停止
- **AND** 不发布 delta/completed、不建立新 claim，也不允许调用方绕到底层 stream seam

### Requirement: 路由按供应商中立能力协商文本流
模型route SHALL使用受信任capability `text_stream`显式声明增量文本能力。Router MUST通过独立`prepare_stream` seam取得`PreparedModelStreamCall`且prepare不发送网络。Legacy单route继续在既有容量、预算、outbox与started证据边界后按原顺序调用prepare。显式route chain则 MUST先完成stream/usage容量与outbox预留、candidate reservation及global attempt started identity提交，再调用`prepare_stream`取得该candidate隔离的permit/client lease；只有这些前置持久化全部成功后才允许send/iterate。Prepare在client/send前确定失败时，必须把同一attempt与`client_not_started` proof原子关闭为`not_started_proven`；started提交后prepare完成/关闭前崩溃或commit-ack未知一律needs-review且不重做prepare、不创建下一attempt、不切provider。既有`text_completion`与legacy `complete`行为不得改变。

#### Scenario: 流式 prepare 不产生网络副作用
- **WHEN** route-chain invocation为支持`text_stream`的candidate完成容量、预算、outbox、reservation与attempt started identity后调用`prepare_stream`
- **THEN** router只取得并持有该candidate的permit/client lease，不发送网络、不消费response stream
- **AND** invocation完成全部前置持久化后才开始iterate；prepare失败用同一attempt的client-not-started proof关闭，崩溃/确认未知不重做prepare

#### Scenario: 一次性调用保持兼容
- **WHEN** 调用使用legacy单route `text_completion` capability
- **THEN** router继续使用既有`prepare`/`complete`协议和permit→client→mark→send顺序
- **AND** route-chain attempt lifecycle不会改变fake、legacy测试double或既有一次性Pydantic AI调用结果

### Requirement: 流式 provider 关闭结果必须可分类
`PreparedModelStreamCall` SHALL继续提供确定性的本地资源关闭并返回provider-neutral `ModelStreamCloseResult`，exact shape、`not_started|stopped|unknown`、nullable `ModelStreamUsage`、usage finality/token/cost/latency校验及SDK隔离均保持Phase 18.1不变。调用方取得iterator不等于provider已开始；SDK stream context创建前可证明未开始时adapter MAY返回`not_started`，context创建后的普通退出、取消、socket关闭或本地超时不得伪造远端停止。适配器始终清理自己拥有的本地任务和client lease，但本地清理不授权预算结算、provider切换或terminal。

Legacy单route中，若双预留尚未提交则`not_started`随UoW回滚；若legacy durable started已发布，则继续保留started、取消stream占位并以not-started cancelled usage final闭合容量和预算，不撤销evidence。显式route chain中，每个attempt started identity早于permit/client/prepare；permit/client/prepare在send/SDK context前以`client_not_started`确定失败时，关闭结果只作为该attempt proof输入：runtime MUST在同一owner UoW保留started identity、追加proof、关闭同一lifecycle为`not_started_proven`，并按冻结retry/transfer决定保留同一stream/usage容量与outbox供下一attempt/candidate使用。该内部候选失败不得套用legacy“取消全部stream占位并发布cancelled final”的调用终态；显式run取消/deadline不得借`not_started`自动跨provider，也不得把prepared对象当作request/provider调用证据。任一chain started lifecycle未关闭、proof/transfer commit-ack未知或SDK context已创建时，关闭结果为unknown/needs-review；只有close result证明`stopped + complete usage`且无durable delta不确定性时，才按actual收敛为`cancelled/invocation_cancelled`并清空reservation与active/waiting/selected，不得切换provider。

#### Scenario: Legacy 未开始即关闭
- **WHEN** legacy调用方已请求首次迭代，但deadline在SDK stream context创建前耗尽，或provider stream尚未开始迭代就被关闭
- **THEN** seam返回`not_started`并释放本地资源
- **AND** 双预留未提交则随UoW回滚；legacy durable started已发布则保留started、取消stream占位并通过not-started cancelled usage final闭合容量和预算

#### Scenario: Chain 资源准备失败只关闭当前 attempt
- **WHEN** chain attempt started identity已提交，permit/client/prepare在send和SDK context创建前以`client_not_started`确定失败，且调用本身未被取消
- **THEN** seam返回`not_started`并释放当前候选本地资源；runtime保留started identity并原子写proof、关闭lifecycle及完成retry/transfer
- **AND** stream group、usage reservation与未消费outbox供冻结后继继续使用，不发布cancelled usage final、不取消全部占位

#### Scenario: Chain 显式取消不授权 fallback
- **WHEN** chain调用因显式run取消或冻结deadline结束，且SDK context创建前可证明`not_started`
- **THEN** runtime不生成candidate not-started proof、不按prepared对象推断provider调用，保留reservation/capacity并进入unknown/needs-review；安全错误允许`provider_called=false/attempt_count>0`
- **AND** 不得把取消解释为candidate failure而启动后继provider；只有另一个逐值完整的`stopped + complete usage`关闭结果才可进入cancelled actual终态

#### Scenario: 本地取消无法证明远端停止
- **WHEN** 已开始的provider stream因task cancellation或连接异常退出，且供应商没有停止确认
- **THEN** seam返回`unknown`
- **AND** invocation保留未决结算与终态围栏；chain不创建下一attempt或candidate

#### Scenario: 已证明停止并返回完整 usage
- **WHEN** provider明确证明远端停止且返回完整、可信的input/output与当前启用cost维度
- **THEN** seam返回`state=stopped`、`usage.finality=complete`的provider-neutral close result
- **AND** invocation可从该DTO生成中断usage evidence，不读取SDK object；chain仅在无durable delta intent/发布不确定性时按可信actual结算为`cancelled/invocation_cancelled`且不fallback，否则needs-review

#### Scenario: unknown 仅携带已观察 usage
- **WHEN** adapter已观察部分token/cost但无法证明远端停止
- **THEN** seam只可返回`state=unknown`、`usage.finality=partial`
- **AND** 该usage只进入attempt审计，不授权结算、退款、lease释放、terminal或provider切换

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

### Requirement: Agent 模型策略公开有序 route refs
Agent descriptor SHALL 允许以 `fallback_routes` 显式启用 chain mode并声明最多 8 个有序 `ModelRouteRef`，每项 exact fields 为 `deployment_id` 与 `model_id`。列表 MUST 非空、不得重复；每个 ref MUST 命中 typed settings 中同 deployment 的 allowed model。缺少该字段的旧 `deployment_id/default_model/fallback_models` 继续使用既有单 route planning fallback；只可生成同 deployment 的只读兼容摘要，不能写 v1 chain state、授权运行时 failover 或授权其他 deployment。

#### Scenario: registry 加载多 deployment policy
- **WHEN** Agent YAML 声明两个不同 deployment 的合法 route refs
- **THEN** registry descriptor 保留原顺序并逐项校验 settings 交集
- **AND** public descriptor 不包含 endpoint、credential、catalog price 或 SDK object

#### Scenario: route ref 未知或重复
- **WHEN** Agent policy 引用未知 deployment/model、重复 ref 或超过 8 项
- **THEN** loader 在 import executor、client 构造与网络前关闭失败

### Requirement: ModelRequest 只能选择冻结 route 子序列
`ModelRequest.route_refs` SHALL 为可选有序 tuple；只有 Agent 显式声明 `fallback_routes` 时才允许存在，并且必须是其非空子序列。显式 chain 即使缩权为单候选也 MUST 保留 `model-route-chain-v1` identity、写 v1 durable state并走 chain evidence/replay，不能降级为 legacy route。Chain identity 中的 `agent_model_policy` MUST 始终逐值复制缩权前原始 Agent descriptor的首-route兼容投影与完整 fallback routes；request只通过 candidates表达删减，不得把兼容投影改写成所选子序列的首 route。`deployment_id/provider/model` 兼容字段若同时存在 MUST 与该子序列的唯一 ref 逐值一致，否则以 `model.request_invalid` 或 `model.route_not_allowed` 拒绝。请求 MUST NOT 携带 endpoint、credential、catalog、Bulkhead 或 provider factory 字段。

#### Scenario: 请求删减中间候选
- **WHEN** Agent route refs 为 A、B、C 而 request 为 A、C
- **THEN** Router 只冻结 A、C 且保持 relative order

#### Scenario: 请求只保留非首候选
- **WHEN** Agent fallback routes为 A、B且 descriptor兼容投影绑定 A，request只保留 B
- **THEN** candidates只含 B，但 chain identity中的 Agent投影仍为 A、完整授权仍为 A、B
- **AND** current/snapshot serializer把错误的 B投影或 `[B]`授权列表视为 identity冲突

#### Scenario: 请求扩大控制面
- **WHEN** request 插入 Agent 未授权 ref、反转顺序或提交 endpoint/credential/provider factory 字段
- **THEN** DTO/Router 在任何预算、client 或网络前拒绝

### Requirement: Agent 策略不能跨候选继承 Policy/HITL 结论
每个 route ref SHALL 保留自身 deployment、model 与预算上界，并在实际进入该 ordinal 前独立执行 Policy/HITL。前一候选的 allow、require approval 或 grant MUST NOT 被复制到后继；waiting 时的 request binding 必须包含 chain id、candidate ordinal、route digest 与原始请求 identity，approval lease 提交后另以 grant binding 绑定 approval/lease。两阶段 binding 均只能缩权，且不得虚构跨 approval/shared-budget repository 原子事务。

#### Scenario: 第二候选要求独立审批
- **WHEN** 第一候选 allow 后以 `client_not_started|trusted_business_not_started` 安全收敛，而第二候选 policy 返回 require approval
- **THEN** runtime 暂停在第二候选且不调用第二或后续 provider
- **AND** 第一候选的 policy 结论不能作为第二候选授权

### Requirement: 业务执行器只通过可信绑定入口选择已注册 schema
Runtime composition SHALL 向 `ModelInvocationService` 注入窄的只读 schema resolver，并由 `build_execution_context()` 把结构化入口绑定到可信 `agent_id`。业务 executor 不能从 service mapping 取得可修改 catalog、Python model 或任意 schema 注册能力；未知 Agent/schema 或 identity 冲突必须在 provider 副作用前关闭失败。

#### Scenario: Bound structured 调用自动取得当前 Agent schema
- **WHEN** executor 从 `build_execution_context()` 取得 model invocation 并调用 `complete_structured`
- **THEN** service SHALL 只解析 context 中绑定 Agent 的 output schema，业务 payload 不能替换 agent id 或 schema definition

#### Scenario: 伪造 schema 或 agent 失败
- **WHEN** executor 尝试提交其他 Agent 的 identity、未知 schema ref 或篡改 digest
- **THEN** 调用 SHALL 稳定拒绝且零 provider 副作用、零 schema catalog 变更
