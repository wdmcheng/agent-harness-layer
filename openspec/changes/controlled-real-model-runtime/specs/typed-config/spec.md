## ADDED Requirements

### Requirement: 真实文本模型 deployment 通过 typed settings 完整装载
`ModelSettings` SHALL 以稳定 `deployment_id` 为键装载 deployment mapping，并声明唯一 `default_deployment_id`；同时 SHALL 以 canonical policy ref 装载唯一受信 `endpoint_policies: dict[str, ModelEndpointPolicySettings]`。每个 endpoint policy entry MUST 冻结非空 policy version、适用 `provider_kind`、非空 exact `allowed_origins`、允许的 completion classifier ref/version 集合，以及可选 provider-default endpoint catalog ref/version。该 mapping 只从品牌 typed settings/受控 overrides 进入 composition；Agent descriptor、request 与 provider 原生 ambient env MUST NOT 创建或修改 entry。`deployment_id`、`credential_ref` 与 `endpoint_policy_ref` MUST 是长度不超过 64 的 canonical lower snake identifier，匹配 `[a-z][a-z0-9]*(?:_[a-z0-9]+)*`；YAML key MUST 已是 canonical form，品牌 env path segment 可大小写不敏感输入，但在冲突检测与 merge 前 MUST 先规范化。每个真实 deployment MUST 包含 `provider_kind`、非空且去重的 `allowed_models`、每个 allowed model 对唯一 `model_catalog_ref/version` 的引用、位于 allowlist 内的 `default_model` 与 `fallback_models`、可选 `base_url`、`endpoint_policy_ref`、非空 `endpoint_policy_version`、`credential_ref`、connect/read/total timeout、有限 `max_attempts`、逐状态 retry/backoff、可选 `completion_classifier_ref/version`、最大并发与排队时限、正整数 `max_prompt_utf8_bytes` 与 `max_output_tokens`、静态 `max_per_attempt_token_bound`/`max_per_attempt_cost_bound` ceiling 和 capability flags；不得复制具有权威性的 strategy、envelope 或价格字段。受控真实非流式文本模型 route 从 exact model catalog 解析的 strategy identity 唯一允许 ref=`utf8-bytes-plus-envelope`、version=`v1`；catalog MUST 证明单 user prompt、无 instructions/message history/tools 时 billable input tokens 不超过 prompt UTF-8 bytes 加冻结 envelope bound。静态 token ceiling MUST 等于 `max_prompt_utf8_bytes+catalog.input_envelope_token_bound+max_output_tokens`；cost 启用时静态 cost ceiling MUST 等于 `(max_prompt_utf8_bytes+catalog.input_envelope_token_bound)*catalog.input_token_price_usd+max_output_tokens*catalog.output_token_price_usd` 的有限非负 Decimal 结果，cost disabled 时为 null。Deployment 的 endpoint policy ref/version、provider kind、canonical origin 与 classifier MUST 逐值命中唯一 policy entry；unknown ref/version 或任一 mismatch MUST fail closed。`base_url` 缺省时，该 policy MUST 绑定 `config/model_endpoints.py` 的版本化只读 provider-default endpoint catalog ref/version，并先由该 catalog 解析 canonical HTTPS URL/origin，再执行相同的 allowlist、credential-origin、digest 与 snapshot 校验；不得采用 SDK/env 动态默认。首个真实 provider kind 只允许非流式 `text_completion`。`fake` deployment MUST 显式存在且不要求 endpoint identity、credential、真实 model catalog 或网络。

HTTP response 自动重试只有在 endpoint policy 显式绑定版本化 completion classifier 时可启用。`MOD-002` execution seam 唯一受支持的 classifier identity 是 ref=`trusted_response_header_not_started`、version=`v1`：仅信任该 endpoint policy 下原始响应中恰好一个、大小写不敏感名称为 `X-Agent-Harness-Completion-State` 且去除 OWS 后值逐字为 `not-started` 的 header；header 缺失、重复、逗号合并、多值、其他值、非法编码、非当前 endpoint policy/version 或来自 body/SDK exception message 时一律分类为 `unknown`。未配置 classifier 的 deployment（包括默认官方 OpenAI endpoint policy）MUST 将 `retryable_http_statuses` 设为空，不得对 429/5xx response 自动 retry；传输层可证明 request 尚未发送的 `not_started` 异常仍可在 `max_attempts` 内重试。配置 classifier 时，endpoint policy/version MUST 显式允许该 classifier，且 classifier identity 必须进入 route/snapshot 与 endpoint policy digest。

Loader SHALL 在解析 typed endpoint policy/default catalog、endpoint 规范化和 credential-origin 校验后确定性计算 `endpoint_policy_digest=sha256(canonical_json)`；canonical JSON 固定为 UTF-8、键排序、无多余空白的 `{"allowed_origins":[...],"canonical_base_url":"...","completion_classifier_ref":null|string,"completion_classifier_version":null|string,"credential_ref":"...","default_endpoint_catalog_ref":null|string,"default_endpoint_catalog_version":null|string,"endpoint_policy_ref":"...","endpoint_policy_version":"...","provider_kind":"...","schema_version":"endpoint-policy/v1"}`，`allowed_origins` 先 canonical 化、去重并按字节排序。未启用 classifier 或 default catalog 时对应 ref/version 必须同时为 JSON null，启用时必须同时为非空 string。Digest 不含 credential value。相同输入必须逐字得到相同 digest；base path、allowed origin、provider kind、credential ref、completion classifier/default catalog identity、policy ref/version 任一变化必须改变 digest。`endpoint_policy_version`、catalog 或 classifier 语义变化必须显式更新版本，loader 仍以 digest 防止漏改。

#### Scenario: YAML 与品牌 env 合并真实 deployment
- **WHEN** profile YAML 提供 deployment 非敏感基线，`.env`、secret file、direct `AGENT_HARNESS_*` env 与受控 overrides 提供非冲突覆盖
- **THEN** loader 按 profile YAML → agent YAML → `.env` → secret file → process env → overrides 的公开顺序返回完整 typed deployment，list 整体替换、mapping 递归合并且 direct/`_FILE` 冲突仍先于 overrides 失败

#### Scenario: Deployment 内部引用不一致
- **WHEN** default deployment 不存在，default/fallback model 不在 deployment allowlist，allowed model 缺 exact model catalog ref/version、catalog provider/model/request-shape/strategy 不匹配、prompt/output cap 非正、配置的每 attempt token/cost 上界与 catalog 公式不一致、attempt 上界乘法溢出，retry/deadline/bulkhead 非有限正边界，启用 response status retry 却未绑定受 endpoint policy 允许的 exact completion classifier，或 capability 不包含 `text_completion`
- **THEN** application startup 返回 `config.invalid` 和安全 `model.*` field path，不创建 provider client、不连接 storage/queue、不发起 DNS/HTTP 请求

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
- **WHEN** local profile 只配置显式 `fake` deployment 并运行默认 quality、unit、contract、eval 或 smoke-local
- **THEN** settings 在没有真实 credential、真实 endpoint 或外部网络的情况下通过校验，且不会因宿主机存在 provider 原生环境变量而改变 deployment

### Requirement: 模型输入上界与价格只从受信 typed catalog 解析
`ModelSettings` SHALL 以 canonical ref 装载 `model_catalogs: dict[str, ModelCatalogEntrySettings]` 作为真实模型输入上界与价格的唯一受信来源。每个 entry MUST 以 ref + 非空 version 冻结 `provider_kind`、model、`request_shape_ref="single-user-text-no-tools"`、`request_shape_version="v1"`、`input_bound_strategy_ref="utf8-bytes-plus-envelope"`、`input_bound_strategy_version="v1"`、非负 `input_envelope_token_bound`、`cost_enabled`、input/output token price、price source ref/version 与 `model-catalog/v1` canonical digest。Cost enabled 时两项价格 MUST 为有限非负值且 price source ref/version MUST 为非空 identity；cost disabled 时两项价格与 price source ref/version MUST 同时为 null，且不得被解释为零价、沿用陈旧来源或绕过 token 维度。每个真实 deployment 的 allowed model MUST 只引用一个 exact catalog ref/version；不得另行声明具有权威性的 envelope 或价格。Loader MUST 通过 `config/model_catalog.py` 逐值解析 provider/model/request-shape/strategy/price/source/digest 并形成 frozen route inputs。Unknown ref/version、provider/model/request-shape/strategy mismatch、digest 漂移、复制值低报或高报、bool/负数/非有限价格或 envelope MUST 在 reservation、Bulkhead、client 与网络前返回 `config.invalid` 或 `budget.reservation_rejected`。Route、snapshot、operation identity 与公开 evidence MUST 冻结 catalog ref/version/digest 和解析值；reload 只影响新 root，恢复不得从 current catalog 补齐或改价。显式 `fake` deployment 保留既有离线零成本 route，不要求真实 model catalog，也不得借 fake catalog 进入真实 composition。

`model_catalog_digest` SHALL 由 canonical JSON 的 SHA-256 计算；canonical JSON 固定为 UTF-8、键排序、无多余空白的 `{"cost_enabled":boolean,"input_envelope_token_bound":integer,"input_bound_strategy_ref":string,"input_bound_strategy_version":string,"input_token_price_usd":null|string,"model":string,"model_catalog_ref":string,"model_catalog_version":string,"output_token_price_usd":null|string,"price_source_ref":null|string,"price_source_version":null|string,"provider_kind":string,"request_shape_ref":string,"request_shape_version":string,"schema_version":"model-catalog/v1"}`。Decimal 价格使用规范十进制字符串，禁止 exponent、尾随无意义零与负零；cost disabled 时四个价格/来源字段必须同时为 JSON null。相同输入必须逐字得到相同 digest，任一权威字段变化都必须改变 digest。

#### Scenario: Deployment 不能自证低报上界或价格
- **WHEN** deployment 引用未知 catalog、其 provider/model 与 entry 不匹配，复制的 envelope/价格与 entry 不同，或试图只让静态 ceiling 与自行低报/高报的值保持内部一致
- **THEN** startup 或 route hard eligibility 在 reservation/permit/client/network 前 fail closed，且不得用 deployment 值替代 catalog 解析值

#### Scenario: Catalog identity 进入 route 与 snapshot
- **WHEN** 合法 deployment/model 形成真实 route 并创建新的 root budget snapshot
- **THEN** route、operation identity 与 `budget-tree-v2` 私有 payload 保存相同 catalog ref/version/digest、request-shape、strategy、envelope 与价格；修改 catalog 只影响新 root，旧 run 不读取 current catalog补齐或改价

#### Scenario: Cost disabled 不伪装零价
- **WHEN** catalog entry 声明 `cost_enabled=false`，但同时提供任一价格/price-source 字段，或声明 cost enabled 却缺少任一权威价格字段
- **THEN** loader 在 route/reservation/client/network 前返回 `config.invalid`；合法 cost-disabled entry 的价格、price-source identity 与 cost reservation 均为 null，token reservation 仍按可信上界执行

### Requirement: 模型 credential reference 复用 typed secret 边界
真实 deployment 的 `credential_ref` SHALL 只引用同一 `ModelSettings.credentials` mapping 中的 `SecretStr` 值；secret 值只可由被忽略的 `.env` 中 `AGENT_HARNESS_*` 字段、direct process env 或受控 `_FILE` 注入。Credential entry MUST 声明允许转发的 exact origin 集合；真实 deployment 的 resolved endpoint origin MUST 位于该集合。Secret 字段 MUST 从 settings serialization、snapshot、descriptor、event、trace、audit、eval、error、health、doctor、数据库和 traceback locals 的可观察输出中排除。

#### Scenario: Direct 与 file credential 冲突
- **WHEN** 同一 credential 同时设置 `AGENT_HARNESS_MODEL__CREDENTIALS__<REF>__VALUE` 与对应 `_FILE`
- **THEN** loader 在读取 secret file、应用 override 或创建 provider client 前返回 `config.secret_file_conflict`，错误不包含 direct value、文件内容或绝对路径

#### Scenario: 大小写别名不能绕过 direct/file 冲突
- **WHEN** direct env 使用 `...CREDENTIALS__Foo__VALUE` 而 file env 使用 `...CREDENTIALS__FOO__VALUE_FILE`，或反向组合
- **THEN** loader 先按 canonical path 比较并返回 `config.secret_file_conflict`，不读取文件、不让任一别名覆盖另一别名

#### Scenario: Credential reference 缺失
- **WHEN** 一个非 fake deployment 引用不存在、空值或未解析的 credential
- **THEN** application startup 返回 `config.invalid` 和该 deployment 的安全 field path，不回显 ref 对应值且零 provider/DNS/HTTP 副作用

#### Scenario: Provider 原生 ambient env 被忽略
- **WHEN** `.env` 或 process env 只设置 `OPENAI_API_KEY`、admin key、base URL、organization、project、webhook、custom headers 或 proxy 等非 `AGENT_HARNESS_*` provider 原生变量
- **THEN** loader 不把它们映射为 credential、endpoint 或 identity，真实 deployment 仍按品牌字段未解析失败，fake/local 路径不使用这些值；合法真实 route 的 SDK ambient 出站隔离由 runtime composition requirement 继续约束

### Requirement: Endpoint 与 credential forwarding policy 在 startup fail closed
配置加载 SHALL 把每个 `base_url` 解析为 canonical URL 和 exact origin，并拒绝 userinfo、query、fragment、非 HTTP(S)、未批准 origin、credential 绑定 origin 不一致以及正式 profile 的明文 HTTP。仅 local profile 可在 deployment 显式启用时接受 literal loopback IP 的 HTTP endpoint；redirect、proxy ambient env 或运行时请求不得扩大已冻结 origin。

#### Scenario: 正式 profile 只接受批准 HTTPS origin
- **WHEN** service profile 配置受 allowlist 和 credential origin 同时批准的 HTTPS `base_url`
- **THEN** startup 保存不含 credential 的 canonical base URL 与 endpoint origin，并允许 composition 在后续显式构造 client

#### Scenario: 恶意或越界 URL 被拒绝
- **WHEN** `base_url` 含 userinfo、query、fragment、非批准 scheme/origin、dot-segment 越界，或 credential 绑定 origin 与 endpoint 不同
- **THEN** startup 返回 `config.invalid`，诊断只包含安全 field path 和 canonical origin 摘要，且在 SDK client、DNS、proxy 或 HTTP 副作用前终止

#### Scenario: Local loopback HTTP 必须双重显式
- **WHEN** local profile 同时显式启用 loopback HTTP 且 endpoint host 是 literal loopback IP
- **THEN** startup 可接受该 origin；相同 URL 在 service profile、使用非 loopback host 或未启用例外时均被拒绝
