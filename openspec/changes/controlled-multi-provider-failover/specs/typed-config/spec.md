## ADDED Requirements

### Requirement: 多 deployment route refs 在 composition 前完整校验
typed settings 与 Agent loader SHALL 在 provider/client 构造前验证所有 multi-provider route refs。每条 ref MUST 命中存在的 deployment 与 allowed model；每个 deployment 的 endpoint/credential forwarding、catalog、capability、deadline/retry、Bulkhead 和价格公式 MUST 独立有效。route refs MUST 非空、唯一且最多 8 项。一个合法候选不得补齐或覆盖另一个候选缺失的安全字段。

#### Scenario: 第二候选 credential 或 catalog 无效
- **WHEN** 首候选合法但第二候选 credential origin、catalog digest 或价格公式无效
- **THEN** 配置/registry 加载整体在 composition client/network 前失败
- **AND** 不因首候选合法而忽略坏候选

#### Scenario: 同 provider kind 的两个 deployment
- **WHEN** 两个 deployment 都是 openai-compatible 但 endpoint/credential/Bulkhead 不同
- **THEN** typed config 保留两个独立 deployment identity
- **AND** 不合并为一个 provider-level client 或 semaphore

### Requirement: 双真实 deployment smoke 具有独立前置与状态
真实 failover smoke SHALL 输出 `model-failover-live-smoke/v1` 四分支 exact判别联合。顶层字段只允许 `schema_version/status/provider_called/attempt_count/chain_id/selected_ordinal/candidates/usage/reason_code`；status封闭为 `passed|hosted-unverified|external-blocked|failed`，provider_called为bool，attempt_count为非bool非负整数，chain id为nullable 64位小写十六进制，selected ordinal为nullable非bool正整数。candidate exact fields只允许 `ordinal/deployment_id/provider/model/outcome/attempt_count/not_started_proof_count/request_sent/response_observed/not_started_reason/http_status`；ordinal必须从1连续唯一升序，三个identity为非空字符串，两个count为非bool非负整数且顶层count等于其总和。outcome只允许`not_started|completed|unknown|not_called`：not-called强制零count/false观察/null reason与status，not-started强制attempt count大于0且proof count相等，其他outcome的proof count为0且reason为null。client-not-started只允许false/false/null；trusted-business-not-started只允许true/true/显式白名单状态；HTTP status为nullable非bool 100～599整数。usage为null或只含input/output tokens、cost与cost status；token为非bool非负整数，cost status只允许`reported|estimated|unavailable`，cost为非bool有限非负number或null且null当且仅当unavailable，逐值复用API 5.29。拒绝unknown fields、负数、bool数字、NaN/Inf、ordinal/count/status非法组合及artifact与durable route-chain evidence不一致。

验收探针 SHALL 冻结恰好两个不同`deployment_id`、不同`credential_ref`和不同受信endpoint，且两条 smoke route均为`max_attempts=1`；两个deployment的`provider_kind` MAY相同。`passed`唯一允许`provider_called=true/attempt_count=2/chain_id=<非空64位摘要>/selected_ordinal=2/reason_code=null`、两个candidate ordinal逐值为[1,2]；首项为not-started/1 attempt/1 proof且命中两类可信组合之一，次项为completed/1 attempt/0 proof、request/response均true、not-started reason为空且HTTP status为2xx，唯一completed命中`selected_ordinal`；usage完整。producer/validator MUST以同一durable chain evidence逐值核对`chain_id`、candidate identity/outcome、两个全局attempt、首项proof、`selected_ordinal`与usage，并以同`provider_kind`双deployment正合同及任一deployment/credential/endpoint复用负合同锁定隔离而非kind差异。缺授权、opt-in、隔离credential/deployment/endpoint对或受信fixture时 MUST为hosted-unverified，且`provider_called=false/attempt_count=0/chain_id=null/selected_ordinal=null/candidates=[]/usage=null`、进程0/CI skipped、零调用；`reason_code`按授权、opt-in、credential pair、deployment pair、fixture顺序只报告最高优先级，并精确为`authorization_missing|failover_opt_in_missing|credential_pair_missing|deployment_pair_invalid|not_started_fixture_missing`之一。

四分支按`status`与identity形状判别：`hosted-unverified`使用前置缺失空形状；`status=failed/reason_code=contract_failure`另允许且只允许chain冻结前 exact子变体`provider_called=false/attempt_count=0/chain_id=null/selected_ordinal=null/candidates=[]/usage=null`。除此子变体外，`passed|external-blocked|failed`都强制`chain_id`非空、`candidates`恰为上述两个冻结identity/ordinal；`provider_called`逐值复制durable invocation安全事实，任一request/response观察强制其为true，false则全部观察为false，response observed另蕴含request sent。`external-blocked`的`reason_code`只允许`network_unavailable|provider_rejected|quota_blocked|provider_timeout|provider_result_unknown`，并强制`selected_ordinal=null`且不得含completed；chain冻结后的本地failed只允许`reason_code=contract_failure`，必须保留两个candidate与已观察facts，`selected_ordinal`若非空须命中唯一completed。不满足两类可信组合却继续次选属于`failed/contract_failure`。所有非空`usage`与artifact/durable evidence继续逐值一致，failed空/非空identity不得混搭；任何status使用其他reason、`passed`的reason非null或reason/status不匹配都关闭失败。

#### Scenario: 任一前置缺失
- **WHEN** 双凭据、双 deployment、授权、opt-in 或受信 not-started fixture 任一缺失
- **THEN** artifact 为 `hosted-unverified`、`provider_called=false`、`attempt_count=0`
- **AND** credential 内容不被读取、输出或用于网络探测

#### Scenario: 本地恢复合同失败
- **WHEN** provider 已按受控事实执行，但 route-chain state、budget transfer、usage publication 或 recovery 校验失败
- **THEN** artifact 为 `failed/contract_failure` 且如实保留 provider_called/attempt_count
- **AND** 不降格为 external-blocked

#### Scenario: artifact 组合非法
- **WHEN** hosted-unverified 带 provider call/attempt，passed 的 ordinal 不是 `[1,2]`、selected不是2或未命中唯一completed，candidate/top-level attempt count不一致，proof count与not-started冲突，usage token/cost组合非法，artifact与durable chain evidence不一致，或出现unknown field、负数、bool数字、NaN/Inf
- **THEN** artifact validator 关闭失败，required acceptance 不得记 PASS

## MODIFIED Requirements

### Requirement: 真实文本模型 deployment 通过 typed settings 完整装载
`ModelSettings` SHALL 以稳定 `deployment_id` 为键装载 deployment mapping，并声明唯一 `default_deployment_id`；同时 SHALL 以 canonical policy ref 装载唯一受信 `endpoint_policies: dict[str, ModelEndpointPolicySettings]`。每个 endpoint policy entry MUST 冻结非空 policy version、适用 `provider_kind`、非空 exact `allowed_origins`、允许的 completion classifier ref/version 集合，以及可选 provider-default endpoint catalog ref/version。该 mapping 只从品牌 typed settings/受控 overrides 进入 composition；Agent descriptor、request 与 provider 原生 ambient env MUST NOT 创建或修改 entry。`deployment_id`、`credential_ref` 与 `endpoint_policy_ref` MUST 是长度不超过 64 的 canonical lower snake identifier，匹配 `[a-z][a-z0-9]*(?:_[a-z0-9]+)*`；YAML key MUST 已是 canonical form，品牌 env path segment 可大小写不敏感输入，但在冲突检测与 merge 前 MUST 先规范化。每个真实 deployment MUST 包含 `provider_kind`、非空且去重的 `allowed_models`、每个 allowed model 对唯一 `model_catalog_ref/version` 的引用、位于 allowlist 内的 `default_model` 与 `fallback_models`、可选 `base_url`、`endpoint_policy_ref`、非空 `endpoint_policy_version`、`credential_ref`、connect/read/total timeout、有限 `max_attempts`、逐状态 retry/backoff、默认空的 `cross_provider_failover_http_statuses`、可选 `completion_classifier_ref/version`、最大并发与排队时限、正整数 `max_prompt_utf8_bytes` 与 `max_output_tokens`、静态 `max_per_attempt_token_bound`/`max_per_attempt_cost_bound` ceiling 和 capability flags；不得复制具有权威性的 strategy、envelope 或价格字段。受控真实普通文本 route 的 `text_completion` 与 `text_stream` 都从 exact model catalog 解析 strategy identity，唯一允许 ref=`utf8-bytes-plus-envelope`、version=`v1`；catalog MUST 证明单 user prompt、无 instructions/message history/tools 时 billable input tokens 不超过 prompt UTF-8 bytes 加冻结 envelope bound。静态 token ceiling MUST 等于 `max_prompt_utf8_bytes+catalog.input_envelope_token_bound+max_output_tokens`；cost 启用时静态 cost ceiling MUST 等于 `(max_prompt_utf8_bytes+catalog.input_envelope_token_bound)*catalog.input_token_price_usd+max_output_tokens*catalog.output_token_price_usd` 的有限非负 Decimal 结果，cost disabled 时为 null。Deployment 的 endpoint policy ref/version、provider kind、canonical origin 与 classifier MUST 逐值命中唯一 policy entry；unknown ref/version 或任一 mismatch MUST fail closed。`base_url` 缺省时，该 policy MUST 绑定 `config/model_endpoints.py` 的版本化只读 provider-default endpoint catalog ref/version，并先由该 catalog 解析 canonical HTTPS URL/origin，再执行相同的 allowlist、credential-origin、digest 与 snapshot 校验；不得采用 SDK/env 动态默认。真实 provider kind MAY 显式声明 `text_completion`、`text_stream` 或两者；Router 必须逐请求 capability 精确匹配，绝不能把 completion 切片伪装成 stream或把 stream collector 冒充 completion。`fake` deployment MUST 显式存在且不要求 endpoint identity、credential、真实 model catalog 或网络。

HTTP response 自动重试或跨 provider failover 只有在 endpoint policy 显式绑定版本化 completion classifier 时可启用。`MOD-002` execution seam 唯一受支持的 classifier identity 是 ref=`trusted_response_header_not_started`、version=`v1`：仅信任该 endpoint policy 下原始响应中恰好一个、大小写不敏感名称为 `X-Agent-Harness-Completion-State` 且去除 OWS 后值逐字为 `not-started` 的 header，并把该语义解释为业务执行与计费均未开始；header 缺失、重复、逗号合并、多值、其他值、非法编码、非当前 endpoint policy/version 或来自 body/SDK exception message 时一律分类为 `unknown`。每个 deployment 新增默认空的 `cross_provider_failover_http_statuses`，只允许去重的 403、429 或 500～599；非空时必须绑定上述 classifier，进入 retry policy digest 与冻结 route/snapshot。403、429、5xx 都不因状态码本身自动受信：只有状态在该显式列表、classifier 合法且无 response identity/usage/text/delta 时才产生 `trusted_business_not_started`；403 默认不启用，Harness `PolicyEngine` deny 永远不映射为该状态。未配置 classifier 的 deployment（包括默认官方 OpenAI endpoint policy）MUST 将 `retryable_http_statuses` 与 `cross_provider_failover_http_statuses` 都设为空，不得对 response 自动 retry/failover；传输层可证明 request 尚未发送的 `not_started` 异常仍可在 `max_attempts` 内重试。若一个状态同时可同 route retry和跨 provider failover，runtime MUST 先消耗当前候选冻结的 attempt policy，耗尽后才推进下一 ordinal。

Loader SHALL 在解析 typed endpoint policy/default catalog、endpoint 规范化和 credential-origin 校验后确定性计算 `endpoint_policy_digest=sha256(canonical_json)`；canonical JSON 固定为 UTF-8、键排序、无多余空白的 `{"allowed_origins":[...],"canonical_base_url":"...","completion_classifier_ref":null|string,"completion_classifier_version":null|string,"credential_ref":"...","default_endpoint_catalog_ref":null|string,"default_endpoint_catalog_version":null|string,"endpoint_policy_ref":"...","endpoint_policy_version":"...","provider_kind":"...","schema_version":"endpoint-policy/v1"}`，`allowed_origins` 先 canonical 化、去重并按字节排序。未启用 classifier 或 default catalog 时对应 ref/version 必须同时为 JSON null，启用时必须同时为非空 string。Digest 不含 credential value。相同输入必须逐字得到相同 digest；base path、allowed origin、provider kind、credential ref、completion classifier/default catalog identity、policy ref/version 任一变化必须改变 digest。`endpoint_policy_version`、catalog 或 classifier 语义变化必须显式更新版本，loader 仍以 digest 防止漏改。

#### Scenario: YAML 与品牌 env 合并真实 deployment
- **WHEN** profile YAML 提供 deployment 非敏感基线，`.env`、secret file、direct `AGENT_HARNESS_*` env 与受控 overrides 提供非冲突覆盖
- **THEN** loader 按 profile YAML → agent YAML → `.env` → secret file → process env → overrides 的公开顺序返回完整 typed deployment，list 整体替换、mapping 递归合并且 direct/`_FILE` 冲突仍先于 overrides 失败

#### Scenario: Deployment 内部引用不一致
- **WHEN** default deployment 不存在，default/fallback model 不在 deployment allowlist，allowed model 缺 exact model catalog ref/version、catalog provider/model/request-shape/strategy 不匹配、prompt/output cap 非正、配置的每 attempt token/cost 上界与 catalog 公式不一致、attempt 上界乘法溢出，retry/deadline/bulkhead 非有限正边界，response retry/cross-provider status 非法或非空却未绑定受 endpoint policy 允许的 exact completion classifier，或 capability 不包含当前请求需要的 `text_completion|text_stream`
- **THEN** application startup 或 route hard eligibility 返回 `config.invalid`/`model.capability_unsupported` 和安全 `model.*` field path，不创建 provider client、不发起 DNS/HTTP 请求；startup 配置合法时不得因另一个 deployment 的不同 capability 拒绝整个 registry

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
