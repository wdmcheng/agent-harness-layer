## MODIFIED Requirements

### Requirement: 真实文本模型 deployment 通过 typed settings 完整装载
`ModelSettings` SHALL 以稳定 `deployment_id` 为键装载 deployment mapping，并声明唯一 `default_deployment_id`；同时 SHALL 以 canonical policy ref 装载唯一受信 `endpoint_policies: dict[str, ModelEndpointPolicySettings]`。每个 endpoint policy entry MUST 冻结非空 policy version、适用 `provider_kind`、非空 exact `allowed_origins`、允许的 completion classifier ref/version 集合，以及可选 provider-default endpoint catalog ref/version。该 mapping 只从品牌 typed settings/受控 overrides 进入 composition；Agent descriptor、request 与 provider 原生 ambient env MUST NOT 创建或修改 entry。`deployment_id`、`credential_ref` 与 `endpoint_policy_ref` MUST 是长度不超过 64 的 canonical lower snake identifier，匹配 `[a-z][a-z0-9]*(?:_[a-z0-9]+)*`；YAML key MUST 已是 canonical form，品牌 env path segment 可大小写不敏感输入，但在冲突检测与 merge 前 MUST 先规范化。

每个真实 deployment MUST 包含 `provider_kind`、非空且去重的 `allowed_models`、每个 allowed model 对唯一 `model_catalog_ref/version` 的引用、位于 allowlist 内的 `default_model` 与 `fallback_models`、可选 `base_url`、`endpoint_policy_ref`、非空 `endpoint_policy_version`、`credential_ref`、connect/read/total timeout、有限 `max_attempts`、逐状态 retry/backoff、默认空的 `cross_provider_failover_http_statuses`、可选 `completion_classifier_ref/version`、最大并发与排队时限、正整数 `max_prompt_utf8_bytes` 与 `max_output_tokens`、静态 `max_per_attempt_token_bound`/`max_per_attempt_cost_bound` ceiling、只接受 `text_completion|text_stream|structured_output|tool_intent` 的去重 capability 集合，以及非 bool 整数 `max_structured_repair_attempts=0..2`；不得复制具有权威性的 strategy、envelope、工具目录字节上限或价格字段。包含 `tool_intent` 的真实 deployment MUST 以该 capability 作为唯一 capability，不得与 text/stream/structured 混合；其 `fallback_models`、同 route retry statuses、`cross_provider_failover_http_statuses` MUST 为空，`completion_classifier_ref/version` MUST 同为 null，`max_attempts` MUST 为 1，`max_structured_repair_attempts` MUST 为 0，Agent MUST NOT 为该调用声明 `fallback_routes`。这保证每个 deployment/model 的唯一 catalog ref 不会同时解释成 no-tools 与 tool-enabled 两种 request shape，也不在本 change 引入 tool-intent route chain、response retry 或 structured repair。

受控真实普通文本 route 的 `text_completion` 与 `text_stream`，以及未声明 `fallback_routes` 的 legacy 非流式单 route `structured_output`，都从 exact `model-catalog/v1` 解析 strategy identity，唯一允许 request shape=`single-user-text-no-tools/v1`、strategy=`utf8-bytes-plus-envelope/v1`。Catalog MUST 证明单 user prompt、无 instructions/message history/tools 时 billable input tokens 不超过实际提交字符串 UTF-8 bytes 加冻结 envelope bound。静态 token ceiling MUST 等于 `max_prompt_utf8_bytes+catalog.input_envelope_token_bound+max_output_tokens`；cost 启用时静态 cost ceiling MUST 等于 `(max_prompt_utf8_bytes+catalog.input_envelope_token_bound)*catalog.input_token_price_usd+max_output_tokens*catalog.output_token_price_usd` 的有限非负 Decimal 结果，cost disabled 时为 null。Text capability 下 cap 约束原始 user prompt；structured capability 下 cap 约束包含 business prompt、完整 canonical schema、phase/ordinal 与稳定 validation codes 的 `structured-provider-prompt-v1` 完整 UTF-8 字符串，且每 attempt reservation 固定使用该 cap 而非业务 prompt 实际长度。Initial 与所有允许 repair ordinal 的最大 code 集合 prompt 必须在 planning 时可构造并逐个不超过 cap，否则 `model.input_too_large` 零调用拒绝。

`tool_intent` deployment MUST 从 exact `model-catalog/v2` 解析 request shape=`single-user-text-with-tool-catalog/v1`、strategy=`utf8-bytes-plus-envelope/v1` 与 `max_tool_catalog_utf8_bytes`。静态 token ceiling MUST 等于 `max_prompt_utf8_bytes+catalog.max_tool_catalog_utf8_bytes+catalog.input_envelope_token_bound+max_output_tokens`；cost 启用时静态 cost ceiling MUST 等于 `(max_prompt_utf8_bytes+catalog.max_tool_catalog_utf8_bytes+catalog.input_envelope_token_bound)*catalog.input_token_price_usd+max_output_tokens*catalog.output_token_price_usd`。动态 route planning 先拒绝超过 prompt cap 或 catalog byte cap 的输入，再以实际 canonical provider catalog bytes 计算可信输入和 reservation；deployment、Agent 或 request 不得复制或覆盖 catalog max。

Deployment 的 endpoint policy ref/version、provider kind、canonical origin 与 classifier MUST 逐值命中唯一 policy entry；unknown ref/version 或任一 mismatch MUST fail closed。`base_url` 缺省时，该 policy MUST 绑定 `config/model_endpoints.py` 的版本化只读 provider-default endpoint catalog ref/version，并先由该 catalog 解析 canonical HTTPS URL/origin，再执行相同的 allowlist、credential-origin、digest 与 snapshot 校验；不得采用 SDK/env 动态默认。真实 provider kind MAY 显式声明三种既有 capability 的任意非空去重子集，或声明 singleton `tool_intent`；Router 必须逐请求 capability 精确匹配，绝不能把 completion 切片伪装成 stream、把 stream collector 冒充 completion、把普通文本结果冒充结构化成功，或把 no-tools route 冒充 tool-intent route。`max_structured_repair_attempts`、最终请求缩小后的 repair limit、`provider_request_limit`、structured full-prompt cap与联合 token/cost reservation，以及 tool-intent request/catalog identity MUST 进入 current plan、budget snapshot 和 durable route evidence；恢复不得从 reload 后的配置补齐。`fake` deployment MUST 显式存在且不要求 endpoint identity、credential、真实 model catalog 或网络。

HTTP response 自动重试或跨 provider failover 只有在 endpoint policy 显式绑定版本化 completion classifier 时可启用，且本段 classifier 只服务既有 `text_completion|text_stream` 路径。`MOD-002` execution seam 唯一受支持的 classifier identity 是 ref=`trusted_response_header_not_started`、version=`v1`：仅信任该 endpoint policy 下原始响应中恰好一个、大小写不敏感名称为 `X-Agent-Harness-Completion-State` 且去除 OWS 后值逐字为 `not-started` 的 header，并把该语义解释为业务执行与计费均未开始；header 缺失、重复、逗号合并、多值、其他值、非法编码、非当前 endpoint policy/version 或来自 body/SDK exception message 时一律分类为 `unknown`。每个 deployment 的 `cross_provider_failover_http_statuses` 只允许去重的 403、429 或 500～599；非空时必须绑定上述 classifier，进入 retry policy digest 与冻结 route/snapshot。403、429、5xx 都不因状态码本身自动受信：只有状态在该显式列表、classifier 合法且无 response identity/usage/text/delta 时才产生 `trusted_business_not_started`；403 默认不启用，Harness `PolicyEngine` deny 永远不映射为该状态。未配置 classifier 的 deployment（包括默认官方 OpenAI endpoint policy）MUST 将 `retryable_http_statuses` 与 `cross_provider_failover_http_statuses` 都设为空，不得对 response 自动 retry/failover；传输层可证明 request 尚未发送的 `not_started` 异常仍可在 `max_attempts` 内重试。若一个状态同时可同 route retry和跨 provider failover，text runtime MUST 先消耗当前候选冻结的 attempt policy，耗尽后才推进下一 ordinal。Structured 一旦调用 send 就固定计为 provider request、停止 transport retry 且不读取 classifier；只有核心 vendor-neutral `StructuredProviderPrepareError(retryable=true)` 能在 send 前推进同 route 下一 transport ordinal。结构化调用只要 Agent policy 显式声明任意非空 `fallback_routes`，即使 request 缩权后只剩一个 candidate，也 MUST 在 usage claim/reservation/attempt/client/send 前以 `model.structured_route_not_allowed` 拒绝，不把显式 chain 降级为 legacy 或利用该机制跨 provider structured fallback。Tool-intent route 本阶段同样不消费 response classifier、不执行同 route retry 或跨 provider fallback；send 后确定性/未知结算遵循 `model-usage-evidence`，不得以 text classifier 证明工具请求未开始。

Loader SHALL 在解析 typed endpoint policy/default catalog、endpoint 规范化和 credential-origin 校验后确定性计算 `endpoint_policy_digest=sha256(canonical_json)`；canonical JSON 固定为 UTF-8、键排序、无多余空白的 `{"allowed_origins":[...],"canonical_base_url":"...","completion_classifier_ref":null|string,"completion_classifier_version":null|string,"credential_ref":"...","default_endpoint_catalog_ref":null|string,"default_endpoint_catalog_version":null|string,"endpoint_policy_ref":"...","endpoint_policy_version":"...","provider_kind":"...","schema_version":"endpoint-policy/v1"}`，`allowed_origins` 先 canonical 化、去重并按字节排序。未启用 classifier 或 default catalog 时对应 ref/version 必须同时为 JSON null，启用时必须同时为非空 string。Digest 不含 credential value。相同输入必须逐字得到相同 digest；base path、allowed origin、provider kind、credential ref、completion classifier/default catalog identity、policy ref/version 任一变化必须改变 digest。`endpoint_policy_version`、catalog 或 classifier 语义变化必须显式更新版本，loader 仍以 digest 防止漏改。

#### Scenario: YAML 与品牌 env 合并真实 deployment
- **WHEN** profile YAML 提供 deployment 非敏感基线，`.env`、secret file、direct `AGENT_HARNESS_*` env 与受控 overrides 提供非冲突覆盖
- **THEN** loader 按 profile YAML → agent YAML → `.env` → secret file → process env → overrides 的公开顺序返回完整 typed deployment，list 整体替换、mapping 递归合并且 direct/`_FILE` 冲突仍先于 overrides 失败

#### Scenario: Deployment 内部引用不一致
- **WHEN** default deployment 不存在，default/fallback model 不在 deployment allowlist，allowed model 缺 exact model catalog ref/version、catalog provider/model/request-shape/strategy 不匹配、prompt/output/catalog cap 非法、配置的每 attempt token/cost 上界与 capability-specific catalog 公式不一致、attempt 上界乘法溢出，retry/deadline/bulkhead 非有限正边界，response retry/cross-provider status 非法或非空却未绑定受 endpoint policy 允许的 exact completion classifier，capability 未包含当前请求需要的 `text_completion|text_stream|structured_output|tool_intent`，tool_intent 与其他 capability 混用、`max_attempts`不为1、具有 fallback model/route、retry/classifier 或非零 structured repair，或 structured repair 上限非法
- **THEN** application startup 对配置形状错误返回 `config.invalid`；普通 text/tool-intent route hard eligibility 缺少能力返回 `model.capability_unsupported`；`structured_output` 请求缺少 deployment capability 或 provider protocol 时由 structured 协调器在 usage claim、reservation 和 client 前统一返回 `model.structured_capability_unsupported`，底层通用错误不得逸出公开 structured seam。各路径都使用安全 `model.*` field path，不创建 provider client、不发起 DNS/HTTP 请求；startup 配置合法时不得因另一个 deployment 的不同 capability 拒绝整个 registry

#### Scenario: Provider identity 不借 adapter 名称漂移
- **WHEN** loader 装载 `fake` 或 `openai-compatible` deployment
- **THEN** `provider_kind` 同时是 Agent/request 兼容 provider assertion、route `provider` 和绑定 adapter `provider_id` 的唯一稳定值；`pydantic-ai`、`openai` 等实现名不得成为可请求或可持久化的 provider identity

#### Scenario: Endpoint policy identity 可机械复算
- **WHEN** 两个 loader 读取相同 canonical base URL、credential ref、allowed origins、completion classifier ref/version 与 policy ref/version
- **THEN** 两者产生相同 `endpoint-policy/v1` digest；修改同 origin 下的 base path、completion classifier identity 或任一其他 identity 输入都会改变 digest，classifier 只填一半或缺 policy ref/version 时 startup fail closed

#### Scenario: Unknown 或越权 endpoint policy identity fail closed
- **WHEN** deployment 引用未知 policy ref/version，policy provider kind 与 deployment 不同，canonical origin 不在 policy allowlist，classifier 未被该 policy 允许，或缺省 base URL 的 default catalog ref/version 不受只读 catalog 支持
- **THEN** application startup 返回 `config.invalid` 和安全 field path，在连接 storage/queue、构造 client、DNS 或 HTTP 前终止，且不得把 deployment 自报的 ref/version 当作已批准 policy

#### Scenario: 缺省 base URL 使用受控 provider 默认 endpoint
- **WHEN** 真实 deployment 省略 `base_url` 且 provider kind 在冻结 default endpoint catalog 中存在
- **THEN** loader 先解析该版本的官方默认 HTTPS base URL，再校验 origin/credential 并生成 endpoint identity；catalog 缺项、版本不明或 SDK/env 默认值与 catalog 不同都在 client/DNS/HTTP 前 fail closed

#### Scenario: Identifier 与别名冲突在 merge 前失败
- **WHEN** YAML 使用非 canonical key，env path 含空 segment/非法字符，或 `Foo`、`FOO` 等多个原始输入规范化到同一 deployment/credential/path
- **THEN** loader 在读取 secret 内容、应用 overrides 或构造 settings 前返回稳定 `config.invalid` 或 `config.secret_file_conflict`，且不得按来源优先级静默覆盖碰撞值

#### Scenario: Fake deployment 保持离线
- **WHEN** local profile 只配置显式 `fake` deployment并运行默认 quality、unit、contract、eval 或 smoke-local
- **THEN** settings 在没有真实 credential、真实 endpoint 或外部网络的情况下通过校验，且不会因宿主机存在 provider 原生环境变量而改变 deployment

#### Scenario: 有效结构化 deployment 加载
- **WHEN** deployment 显式声明 `structured_output` 与 repair 上限 0、1 或 2
- **THEN** typed settings、route snapshot 和 evidence SHALL 保留相同值，request 只能缩小 repair limit

#### Scenario: 有效工具意图 deployment 加载
- **WHEN** deployment 只声明 `tool_intent`，每个 allowed model 引用匹配的 tool-enabled v2 catalog，`max_attempts=1`、structured repair为0，且 fallback models/routes、retry statuses与classifier均为空
- **THEN** typed settings、route snapshot 与 evidence SHALL 保留 tool-intent capability、request shape、catalog max 和 digest，request 只能缩小工具集合

#### Scenario: 非法能力或 repair 值失败
- **WHEN** capability 未知、重复，tool_intent 与其他 capability 混合，或 repair limit 为 bool、负数、浮点、字符串或大于 2
- **THEN** startup SHALL 以 `config.invalid` 在 client、DNS、HTTP 前失败，不隐式改用默认值

#### Scenario: Reload 不改变旧 run 边界
- **WHEN** 旧 run 已冻结 structured capability/repair limit 或 tool-intent request/catalog identity 后 current settings 被 reload
- **THEN** 旧 run/recovery SHALL 继续使用原 snapshot 值，新值只影响新 root

### Requirement: 模型输入上界与价格只从受信 typed catalog 解析
`ModelSettings` SHALL 以 canonical ref 装载 `model_catalogs: dict[str, ModelCatalogEntrySettings]` 作为真实模型输入上界与价格的唯一受信来源。每个 entry MUST 是以下两个 exact 判别变体之一：

- `model-catalog/v1`：冻结既有 provider kind、model、`request_shape_ref="single-user-text-no-tools"`、`request_shape_version="v1"`、`input_bound_strategy_ref="utf8-bytes-plus-envelope"`、`input_bound_strategy_version="v1"`、非负 `input_envelope_token_bound`、cost/price/source 与 ref/version；MUST NOT 携带或默认补齐 `max_tool_catalog_utf8_bytes`。
- `model-catalog/v2`：只用于 singleton `tool_intent` deployment，除 v1 的公共字段外 MUST 冻结 `request_shape_ref="single-user-text-with-tool-catalog"`、`request_shape_version="v1"` 与非 bool 非负 `max_tool_catalog_utf8_bytes`；不得用于 text/stream/structured capability。

Cost enabled 时两项价格 MUST 为有限非负值且 price source ref/version MUST 为非空 identity；cost disabled 时两项价格与 price source ref/version MUST 同时为 null，且不得被解释为零价、沿用陈旧来源或绕过 token 维度。每个真实 deployment 的 allowed model MUST 只引用一个 exact catalog ref/version；不得另行声明具有权威性的 envelope、工具目录字节上限或价格。Loader MUST 通过 `config/model_catalog.py` 逐值解析 schema version、provider/model/request-shape/strategy/catalog cap/price/source/digest 并形成 frozen route inputs。Unknown ref/version、判别字段混搭、provider/model/request-shape/strategy/capability mismatch、digest 漂移、复制值低报或高报、bool/负数/非有限价格、envelope 或 catalog cap MUST 在 reservation、Bulkhead、client 与网络前返回 `config.invalid` 或 `budget.reservation_rejected`。Route、snapshot、operation identity 与公开 evidence MUST 冻结 catalog ref/version/digest 和解析值；tool-intent 私有 snapshot 还保存 canonical provider catalog bytes。Reload 只影响新 root，恢复不得从 current catalog 补齐或改价。显式 `fake` deployment 保留既有离线零成本 route，不要求真实 model catalog，也不得借 fake catalog 进入真实 composition。

`model_catalog_digest` SHALL 按判别版本从 exact canonical JSON 的 SHA-256 计算。V1 canonical object 固定为 `{"cost_enabled":boolean,"input_envelope_token_bound":integer,"input_bound_strategy_ref":string,"input_bound_strategy_version":string,"input_token_price_usd":null|string,"model":string,"model_catalog_ref":string,"model_catalog_version":string,"output_token_price_usd":null|string,"price_source_ref":null|string,"price_source_version":null|string,"provider_kind":string,"request_shape_ref":"single-user-text-no-tools","request_shape_version":"v1","schema_version":"model-catalog/v1"}`。V2 canonical object只在相同公共字段中新增 `"max_tool_catalog_utf8_bytes":integer`，并固定 `request_shape_ref="single-user-text-with-tool-catalog"`、`request_shape_version="v1"`、`schema_version="model-catalog/v2"`。两种 canonical JSON 都使用 UTF-8、键排序、无多余空白；Decimal 价格使用规范十进制字符串，禁止 exponent、尾随无意义零与负零；cost disabled 时四个价格/来源字段必须同时为 JSON null。相同输入必须逐字得到相同 digest，任一权威字段变化都必须改变 digest。

#### Scenario: Deployment 不能自证低报上界或价格
- **WHEN** deployment 引用未知 catalog、其 provider/model/capability 与 entry 判别变体不匹配，复制的 envelope/工具目录上限/价格与 entry 不同，或试图只让静态 ceiling 与自行低报/高报的值保持内部一致
- **THEN** startup 或 route hard eligibility 在 reservation/permit/client/network 前 fail closed，且不得用 deployment 值替代 catalog 解析值

#### Scenario: Catalog identity 进入 route 与 snapshot
- **WHEN** 合法 deployment/model 形成真实 route 并创建新的 root budget snapshot
- **THEN** route、operation identity 与 `budget-tree-v2` 私有 payload 保存相同 catalog ref/version/digest、schema version、request shape、strategy、envelope、nullable catalog max与价格；tool-intent snapshot还保存匹配digest的provider catalog bytes，修改 catalog 只影响新 root，旧 run 不读取 current catalog补齐或改价

#### Scenario: Cost disabled 不伪装零价
- **WHEN** catalog entry 声明 `cost_enabled=false`，但同时提供任一价格/price-source 字段，或声明 cost enabled 却缺少任一权威价格字段
- **THEN** loader 在 route/reservation/client/network 前返回 `config.invalid`；合法 cost-disabled entry 的价格、price-source identity 与 cost reservation 均为 null，token reservation 仍按可信上界执行

#### Scenario: V1 与 V2 字段不能混搭
- **WHEN** no-tools v1 entry 携带工具目录上限，tool-enabled v2 entry 缺少上限，或 schema/request shape/capability 组合交叉混用
- **THEN** loader 在任何业务 executor、reservation、client 或 provider 副作用前拒绝整个配置

#### Scenario: V2 工具目录上限参与 digest
- **WHEN** 两个 tool-enabled v2 catalog entry 只有 `max_tool_catalog_utf8_bytes` 不同
- **THEN** 两者 model catalog digest 必须不同且 route 不能 exact replay
