## MODIFIED Requirements

### Requirement: AgentRegistry 加载并校验多个 agent descriptor
系统 SHALL 从受控 agent config 目录加载多个 `AgentDescriptor`，并拒绝重复 `agent_id`、无效 schema 或缺少必要字段的配置。每个 `config.yaml` MUST 声明 `agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model.provider`、`model.deployment_id`、`model.allowed_models`、`model.default_model`、`model.fallback_models`、`budget.max_tokens_per_run`、`budget.max_cost_usd_per_run`、`tool_allowlist`、`eval_dataset` 和 `delegation_edges`；default/fallback models MUST 是 allowed models 的子集且不得重复。缺少 `model.fallback_routes` 时保持 legacy 单 deployment 模式，上述 model 字段的既有含义和校验不变。显式提供 `fallback_routes` 时进入 route-chain 模式：它是跨 deployment 授权与顺序的唯一真相源；既有字段只作为首候选的确定性兼容投影，`provider` MUST 等于首候选 deployment 的 `provider_kind`，`deployment_id`/`default_model` MUST 等于首个 ref，`allowed_models` MUST 按 route 顺序去重投影该首 deployment 中已列出的 models，`fallback_models` MUST 为空。兼容投影不得授权未列入 `fallback_routes` 的 route，也不得让后继候选继承首 deployment 的 provider、catalog、credential、capability 或预算。

Agent 的有效 route 只要存在 `tool_intent` capability，`config.yaml` MUST 额外声明 exact `model_tool_loop` 对象，且该对象只允许五个全部必填字段：`max_turns` 为非 bool 的 `1..64` 整数，`max_total_tokens` 为非 bool 正整数且不得超过 `budget.max_tokens_per_run`，`max_total_cost_usd` 为 null 或有限非负数且在根 `budget.max_cost_usd_per_run` 非 null 时不得为 null 或超过根值，`max_tool_output_bytes` 为非 bool 的 `1..1048576` 整数，`max_duration_seconds` 为非 bool 的 `1..3600` 整数。不存在任何默认值、环境变量回退或 deployment 隐式补齐；不能路由到 `tool_intent` 的 Agent MUST 不声明该对象。Registry SHALL 将五项逐值投影为 public descriptor 的只读 `model_tool_loop` summary；summary 不含动态余额、启动时间、deadline、credential 或本地路径。

Registry MUST 在同一次全量加载中把每个 Agent 的 `output_schema` 解析为严格 canonical JSON Schema 与 `output-schema-identity-v1`，并在 descriptor、executor 与全部 schema 都验证成功后原子替换只读 catalog；任一 sibling schema 无效时 MUST 整体拒绝，不能留下部分可运行 catalog。public descriptor SHALL 只暴露 `agent_id`、`version`、`name`、`description`、输入/输出 schema refs、与输出 ref 匹配的 provider-neutral `output_schema_identity`、相对 `config_ref`、tool policy summary、model policy summary、budget summary、可选的只读 `model_tool_loop` summary、eval dataset ref 和 delegation target ids；`output_schema_identity` exact fields 为 `schema_version="output-schema-identity-v1"`、`schema_ref`、descriptor `version` 和严格 canonical JSON Schema 的 64 位小写 SHA-256 `digest`。chain summary SHALL 保留有序 `(deployment_id, model_id)` refs 与上述投影，但 public descriptor MUST NOT 暴露本地绝对路径、provider secret、endpoint、callable、catalog price、provider client、Python class、module object、Pydantic AI 或 provider SDK 类型。

既有 `examples.dev_assistant` 的 `DevAssistantOutput.result` 不得继续使用会生成 `additionalProperties=true` 的 `dict[str, object]`。只有核心schema compiler、catalog与公开structured seam先稳定后，Phase 19才可把它迁移为严格 `DevAssistantToolResult`：仅允许当前read/write/shell完成结果的`path/content/bytes/artifact_ref/exit_code/stdout/stderr/stdout_ref/stderr_ref/duration_ms`字段，字段按实际工具结果保持可选或nullable，未知工具结果字段关闭失败；外层既有status、tool_name、source/artifact/policy/trace引用语义保持不变。该示例迁移只适配Registry严格加载，不定义核心structured DTO，不增加工具执行路径，也不得放宽全量原子失败规则。

既有 `examples.rag_assistant` 的 `RagOutput.assembly_truncation` 不得继续使用会生成schema-valued `additionalProperties` 的 `dict[str, int]`。公开structured seam稳定后，Phase 19 SHALL 将该字段迁移为两个互斥exact object的封闭union，两者都递归`additionalProperties=false`：`RagAssemblyTruncationEmpty`不含任何字段，canonical payload固定为`{}`；`RagAssemblyTruncation`的exact字段只允许`input_count/retained_count/truncated_count/dropped_count/used_tokens/fragment_count`，每项都是必填、非bool、非负整数。`status=no_source`当且仅当使用empty变体，并要求`assembly_id/model_provider=null`、citations/source refs为空；该变体只由检索结果为空且未创建Context Assembly的本地分支构造，不伪造六个零计数或assembly耐久记录。`status=completed`当且仅当使用六字段变体，并要求非空`assembly_id/model_provider`；Executor只从`ContextAssemblyResult.truncation_summary`的同名六个已冻结producer字段构造它。任何部分六字段、empty与completed、六字段与no-source或其他混搭都关闭失败。外层`assembly_truncation`字段名、citation/trust/assembly/model/trace语义和既有RAG离线流程保持不变。该兼容迁移不把RAG示例schema当作SDK核心类型，不改写Context Assembly的耐久字典schema，也不放宽严格compiler。

#### Scenario: 列出已配置 agent
- **WHEN** 调用方通过 CLI 或 API 请求 agent 列表
- **THEN** 系统返回已配置 agent 的 public descriptor 字段、输出 schema identity 和不含 endpoint/credential 的 deployment/model policy summary；chain mode 还按原顺序返回 route refs，支持工具意图的 Agent 还返回逐值匹配配置的只读 `model_tool_loop` summary，且不暴露动态余额、deadline、本地路径、provider secret 或内部对象

#### Scenario: Descriptor 字段契约完整
- **WHEN** registry 加载 smoke agent config
- **THEN** descriptor 包含 `agent_id`、`version`、输入/输出 schema refs、与输出 ref/version/canonical definition 逐值一致的 `output_schema_identity`、相对 `config_ref`、deployment/model 策略、预算、工具白名单摘要、按 capability 判别存在的循环上限摘要、eval dataset 和 delegation edge 列表；显式 chain 的 legacy 投影逐值匹配首候选

#### Scenario: 重复 agent_id 被拒绝
- **WHEN** registry 加载到两个相同 `agent_id` 的配置
- **THEN** registry 失败并返回稳定错误码，错误详情包含冲突的 `agent_id`

#### Scenario: 无效 agent config 被拒绝
- **WHEN** agent config 缺少必要字段、字段类型不合法，legacy default/fallback models 扩大或脱离 allowed models，chain 的兼容投影、route ref 与 typed deployment 不一致，schema reference 逃逸目录、目标缺失、canonical 化失败、允许额外字段，或 `model_tool_loop` 缺失、额外、用于错误 capability、含 bool/非有限数/越界值或扩大根 token/cost 预算
- **THEN** registry 在 executor 导入、client 构造和网络前失败并返回 registry validation error，不创建部分可用的脏 registry，也不应用隐式循环默认值

#### Scenario: Scaffold 生成严格且可离线运行的 fake Agent 配置
- **WHEN** 调用方执行 `agent-harness scaffold agent` 生成新 Agent package
- **THEN** 生成的 `config.yaml` 显式写入 `model.provider=fake`、`model.deployment_id=fake_default`、`model.allowed_models=[fake-scaffold]`、`model.default_model=fake-scaffold` 与空 fallback，且不写 `fallback_routes` 或 `model_tool_loop`；local/service profiles 的 `fake_default` deployment 允许该模型，生成包通过正式 Registry、schema catalog 校验和离线 runtime 执行，且不读取真实 credential 或访问网络

#### Scenario: Chain 兼容字段不能扩权
- **WHEN** `fallback_routes` 为不同 deployment 的 A、B、C，而 legacy `provider/deployment_id/allowed_models/default_model/fallback_models` 不是首 deployment 的规定投影，或试图借 `allowed_models/fallback_models` 加入未列 route
- **THEN** registry 在 executor import、client 构造和网络前关闭失败；合法投影只供旧 reader 摘要使用，Router 仍只接受 A、B、C

#### Scenario: Registry 原子加载 schema 与 executor
- **WHEN** 目录中所有 Agent descriptor、schema 与 executor 均有效
- **THEN** 每个 Agent SHALL 同时可解析 descriptor、executor 与匹配的 output schema identity/definition，排序和重载保持稳定；内置`examples.dev_assistant` SHALL 以严格工具结果DTO通过同一catalog，且read/write/shell既有输出引用与离线流程不退化；`examples.rag_assistant` SHALL 以`no_source + {}`或`completed + 严格六字段组裁摘要`中唯一匹配的封闭union通过catalog，并保持citation/trust/assembly/model/trace与实际组裁计数逐值不变

#### Scenario: 任一 schema 无效则整体失败
- **WHEN** 一个 sibling 的 schema reference 逃逸目录、目标缺失、不是受支持 schema、canonical 化失败或允许额外字段
- **THEN** Registry SHALL 以稳定 `RegistryLoadError` 整体拒绝，其他 Agent 不得以部分 catalog 继续运行

#### Scenario: 工具循环上限无默认值且按能力判别
- **WHEN** Agent 任一有效 route 支持 `tool_intent` 但缺少五项完整 `model_tool_loop`，或 Agent 无该 capability 却声明对象
- **THEN** Registry 在 executor/client/provider 前整体拒绝，不从 deployment、环境或代码常量补齐

#### Scenario: 循环上限不能扩大根预算
- **WHEN** `max_total_tokens` 超过 `budget.max_tokens_per_run`，或根 cost 非 null 而 loop cost 为 null 或更大
- **THEN** Registry 整体拒绝且 public catalog 不替换

## ADDED Requirements

### Requirement: 工具结果只通过 ContextAssembler 进入下一模型轮
绑定模型工具 loop SHALL 将 guarded `ToolCallResult` 转换为 `ContextFragment(kind=tool_result, trust_level=untrusted)`，并通过当前 tenant/run 的 ContextAssembler repository seam 组装下一轮输入。Fragment SHALL 保留 source ref、artifact ref、token estimate、truncation 和 injection summary；Context Assembly SHALL 产生稳定 output ref/digest、input refs、trust/truncation summary 与 fragment trace。Loop MUST NOT 裸拼工具文本、直接读取 handler 原始值或让业务 executor覆盖 trust/source。

#### Scenario: 成功工具结果产生可追踪 assembly
- **WHEN** 工具完成且结果通过output guard
- **THEN** Context Assembly record绑定loop/turn/tool call和result source/artifact refs
- **AND** 下一model turn只使用该assembly的安全输出

#### Scenario: Caller伪造trusted工具结果失败
- **WHEN** 业务输入或provider尝试把tool result标为trusted、删除source ref或替换artifact ref
- **THEN** bound loop在Context Assembly或下一model call前拒绝

#### Scenario: 截断顺序可解释
- **WHEN** history、retrieval和tool fragments共同超过token budget
- **THEN** ContextAssembler按既有可解释顺序裁剪并记录每个fragment trace
- **AND** dropped/truncated工具内容不从其他旁路进入prompt
