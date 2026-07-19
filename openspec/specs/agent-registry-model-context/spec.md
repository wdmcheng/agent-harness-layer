# agent-registry-model-context Specification

## Purpose
定义 Agent Registry、模型路由、ContextAssembler 与 Embedding provider/cache 的长期契约，使多 agent 发现、provider 边界、上下文组装和 embedding 复用有稳定 public seam。
## Requirements
### Requirement: AgentRegistry 加载并校验多个 agent descriptor
系统 SHALL 从受控 agent config 目录加载多个 `AgentDescriptor`，并拒绝重复 `agent_id`、无效 schema 或缺少必要字段的配置。每个 `config.yaml` MUST 声明 `agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model.provider`、`model.default_model`、`model.fallback_models`、`budget.max_tokens_per_run`、`budget.max_cost_usd_per_run`、`tool_allowlist`、`eval_dataset` 和 `delegation_edges`。public descriptor SHALL 只暴露 `agent_id`、`version`、`name`、`description`、schema refs、相对 `config_ref`、tool policy summary、model policy summary、budget summary、eval dataset ref 和 delegation target ids；MUST NOT 暴露本地绝对路径、provider secret、callable 或 provider client。

#### Scenario: 列出已配置 agent
- **WHEN** 调用方通过 CLI 或 API 请求 agent 列表
- **THEN** 系统返回已配置 agent 的 public descriptor 字段，且不暴露本地路径、provider secret 或内部对象

#### Scenario: Descriptor 字段契约完整
- **WHEN** registry 加载 smoke agent config
- **THEN** descriptor 包含 `agent_id`、`version`、输入/输出 schema refs、相对 `config_ref`、模型策略、预算、工具白名单摘要、eval dataset 和 delegation edge 列表

#### Scenario: 重复 agent_id 被拒绝
- **WHEN** registry 加载到两个相同 `agent_id` 的配置
- **THEN** registry 失败并返回稳定错误码，错误详情包含冲突的 `agent_id`

#### Scenario: 无效 agent config 被拒绝
- **WHEN** agent config 缺少必要字段或字段类型不合法
- **THEN** registry 失败并返回 registry validation error，不创建部分可用的脏 registry

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
系统 SHALL 通过 `ModelProvider` interface 和 `ModelRouter` 选择默认/任务级模型，执行 timeout、fallback 和预算估算，并为 provider、budget 配置变更提供显式 reload/restart seam。

#### Scenario: Fake model 不需要真实 API key
- **WHEN** local profile 使用 fake provider 运行 tests 或 smoke
- **THEN** 模型调用成功返回可预测结果，且不读取真实 provider key

#### Scenario: 预算超阈值产生可追踪 fallback decision
- **WHEN** 模型调用预计 token 或 cost 超过配置阈值
- **THEN** `ModelRouter` 返回可追踪的 fallback / policy-needed decision summary，包含估算值、阈值和选定动作

#### Scenario: 业务 agent 不直接 import Pydantic AI
- **WHEN** import boundary check 扫描 runtime core、template app 和业务 agent
- **THEN** `pydantic_ai` import 只允许出现在 `agent_harness.adapters.models.pydantic_ai` 或测试中的受控 adapter seam

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
Root run SHALL在创建且任何业务副作用前冻结tree budget snapshot。该snapshot MUST区分owner envelope与agent sub-snapshots：owner envelope保存`max_tokens_per_run`、`max_cost_usd_per_run`、cost-enabled状态、registry/config/catalog versions和snapshot ID；root agent与当时显式允许的P0 delegation targets各自保存独立descriptor version、model policy、target budget ceiling、允许provider/model routes及price source refs/versions。Child MUST继承同一owner snapshot ID与shared hard limits，并按自身target `agent_id`使用root时刻冻结的对应sub-snapshot，不得继承source agent descriptor或读取reload后的target配置。Target ceiling只能进一步收紧owner已启用维度，不能提高shared hard limit或重新启用owner已关闭的cost维度。Reload MUST只影响新root run。Fallback MAY在当前agent对应的frozen route/price sub-snapshot内按实际route重算trusted reservation，但 MUST NOT修改该run hard limit或使用reload后配置/价格。

#### Scenario: Reload 不改变在途 run
- **WHEN** root run已冻结budget snapshot，随后registry/provider/budget/price配置reload
- **THEN** 该run及其child继续使用原hard limits、config version和price source/version，新root run才使用reload后snapshot

#### Scenario: Fallback 重算 reservation 但不改上限
- **WHEN** 在途run按frozen policy选择另一个允许的fallback route
- **THEN** router使用该route在frozen price source/version下重算trusted reservation，并继续受原frozen parent hard limit约束

#### Scenario: 跨 agent child 使用冻结的 target sub-snapshot
- **WHEN** source agent委派到descriptor/model policy/budget与source不同、但在root创建时显式允许的target agent
- **THEN** child继承同一owner snapshot ID与shared hard limits，同时使用snapshot内该target自己的descriptor/model-policy/route/price版本；source/target descriptor不同不构成冲突，target ceiling只能进一步收紧已启用owner维度

#### Scenario: Target reload 不改变既有 tree
- **WHEN** root创建后target descriptor、model policy、budget或price catalog reload，再创建或恢复该target child
- **THEN** child仍使用root tree snapshot中的target sub-snapshot；未在该snapshot中冻结的target或route在provider/child/queue副作用前拒绝，新root才使用reload后版本
