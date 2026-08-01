# typed-config Specification

## Purpose
定义 profile YAML、agent YAML、env / `.env` 和 explicit overrides 的 typed merge 行为，以及 structured diagnostics 和公开 schema import seam。该 spec 保证 local/service profiles 在不启动外部服务的情况下可校验，并为 template app 和后续 runtime 提供配置契约。
## Requirements
### Requirement: 配置加载器合并 env、profile YAML 和 agent YAML
package SHALL 提供 typed settings loader，把显式 defaults、profile YAML、agent config YAML、`.env`、受控 Docker secret file、environment values 和 explicit overrides 合并成一个已校验 settings object。合并顺序 MUST 为 profile YAML → agent YAML → `.env` → Docker secret file → process env → explicit overrides；所有 secret file 值 MUST 在进入既有 env path parser 后由同一 Pydantic schema 校验。

#### Scenario: Local profile 不需要 provider key
- **WHEN** 调用方加载 `templates/service-app/configs/profiles/local.yaml`
- **THEN** storage、queue、observability、policy、model、budget 和 identity settings 在不需要真实模型或 SaaS provider credentials 的情况下通过校验

#### Scenario: Service profile 校验部署边界
- **WHEN** 调用方加载 `templates/service-app/configs/profiles/service.yaml`
- **THEN** API/worker process settings、shared storage/queue config 和 provider boundary placeholder 都以 typed settings 形式通过校验，且不启动外部服务

#### Scenario: Agent YAML 参与 typed merge
- **WHEN** 调用方提供包含 metadata、budget、tool allowlist、eval dataset 或 delegation edges 的 agent config YAML
- **THEN** 这些值通过 typed schema 校验，并出现在 merged settings object 中

#### Scenario: Docker secret file 映射到既有 typed field
- **WHEN** service process 只设置 `AGENT_HARNESS_STORAGE__DSN_FILE`，其值是默认或显式受信 root 内的合法 secret file 绝对路径
- **THEN** loader 把文件内容映射为 `storage.dsn`，并按与 `AGENT_HARNESS_STORAGE__DSN` 相同的 schema 和 field path 校验

#### Scenario: Direct value 与 file value 冲突
- **WHEN** 同一进程环境同时设置 `<BASE_ENV>` 与 `<BASE_ENV>_FILE`
- **THEN** loader 在读取配置副作用和应用启动前返回结构化冲突错误，不静默选择任一值且不回显 direct value 或文件内容

### Requirement: 校验诊断可操作
配置校验失败 SHALL 暴露安全的逻辑 field path 和 remediation hint，而不是直接抛出原始 parser trace 或公开宿主机文件系统路径。

#### Scenario: 缺少必填 profile 字段
- **WHEN** required nested profile field 缺失
- **THEN** loading 失败，并报告缺失字段路径和指向 profile 或 env variable 的修复建议

#### Scenario: 非法 YAML 被安全报告
- **WHEN** profile 或 agent YAML 无法解析为 mapping
- **THEN** loading 以 structured config error 失败，`field_path` 标出 `profile` 或 `agent` 逻辑来源，错误不公开宿主机绝对路径，且不会执行 arbitrary YAML tags

### Requirement: 配置 schemas 可公开复用
Profile、provider、storage、queue、observability、policy、budget、identity 和 agent config schemas SHALL 可从 `agent_harness.config` import，供 template app 和 tests 复用。

#### Scenario: Template app 通过公共包 import config schemas
- **WHEN** `templates/service-app/app/*` 下的代码需要配置类型
- **THEN** 它从 `agent_harness.config` import，而不是直接读取 YAML 或依赖 provider SDK

### Requirement: Secret file 读取边界 fail-closed
loader SHALL 只读取默认 `/run/secrets` 或测试显式注入的受信 root 内绝对路径所指向的普通、非 symlink 文件。loader MUST 拒绝相对路径、目录、symlink、规范化后越界、不可读、空值、非 UTF-8 和超过 64 KiB 的文件；读取成功时只移除一个结尾换行，其他空白 MUST 保留。

#### Scenario: 合法只读 secret file 被消费
- **WHEN** `_FILE` 指向受信 root 内可读、非空、UTF-8、大小不超过 64 KiB 的普通文件
- **THEN** loader 返回去掉至多一个结尾换行的值，且不在诊断或 evidence 中公开原值

#### Scenario: 路径或文件类型不受信
- **WHEN** `_FILE` 是相对路径、目录、symlink、特殊文件或解析后逃出受信 root
- **THEN** loader 返回 `config.secret_file_invalid`，不读取目标内容，不回显受信 root 外绝对路径

#### Scenario: 内容不满足边界
- **WHEN** secret file 不可读、为空、不是 UTF-8 或超过 64 KiB
- **THEN** loader 返回 `config.secret_file_invalid` 和安全修复提示，错误、日志及公开 evidence 不包含文件内容或 raw exception

### Requirement: Application startup 统一配置失败
CLI、FastAPI、runtime worker 和 migration composition SHALL 在加载缺失、无效或冲突配置时复用同一结构化失败合同，包含稳定 error code、field path 和安全 remediation。配置失败 MUST 在监听端口、连接 storage/queue、运行 migration、创建 run 或发布业务 evidence 前终止；health endpoint MUST NOT 把启动配置失败表示为运行中 `degraded`。

#### Scenario: 四类入口对缺失必填字段一致失败
- **WHEN** 相同 service profile 缺少必填配置并分别启动 CLI composition、FastAPI app、worker 和 migration composition
- **THEN** 四者在外部副作用前失败，并返回相同 code、field path 和不含 secret/绝对路径的修复提示

#### Scenario: Secret fixture 不进入公开观测面
- **WHEN** direct value、secret file 内容或底层异常包含唯一 secret fixture
- **THEN** stdout/stderr、doctor、health、日志、error envelope、trace、eval、audit 和 CanonicalEvent evidence 均不包含该原值

### Requirement: Shared-budget fingerprint key 通过 typed secret 边界注入
Shared-budget tenant-scoped request fingerprint key SHALL 是 `BudgetSettings` 的 typed secret 字段，并由统一配置加载器从 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` 或 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE` 注入。两者同时存在 MUST 返回 `config.secret_file_conflict`；file 入口 MUST 复用受信 secret root、绝对普通非 symlink 文件、最大 64 KiB、UTF-8、非空及只移除一个结尾换行的 CFG-001 规则。四类 application startup 在 key 缺失或非法时 MUST fail closed；`SharedBudgetRuntime` 与 migration MUST NOT 直接读取环境变量、文件路径或自行执行 whitespace normalization。

该 secret 字段 MUST 从 settings `model_dump`/`to_payload`、tree snapshot、event、trace、audit、error、日志、health/doctor、数据库与 traceback frame locals 的可观察输出中排除。Runtime composition MAY 在启动时把 secret 转成仅供 fingerprint 计算的进程内 bytes，但 MUST NOT 持久化或回显原值；数据库仍只保存 opaque fingerprint 与 key version。

#### Scenario: Direct typed secret 正常注入
- **WHEN** application 只设置非空 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY`
- **THEN** typed settings 在 startup 构造 shared-budget runtime，operation identity 使用该 key 计算 opaque fingerprint，settings payload、snapshot、日志与 evidence 均不含原值

#### Scenario: Docker secret file 保留统一内容语义
- **WHEN** application 只设置合法 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE`，文件位于受信 root 且以一个结尾换行结束
- **THEN** loader 只移除该一个结尾换行并注入 typed secret；runtime 不再次 `.strip()`，其余前后空白属于 secret 内容且必须保留

#### Scenario: 缺失或非法 key 在四类启动入口失败
- **WHEN** key 缺失，或 direct/file 冲突，或 file 为相对路径、目录、symlink、越界、空、非 UTF-8、超限或不可读
- **THEN** API、worker、migration startup 与 doctor/CLI application boundary 在 shared-budget runtime 接受请求前结构化失败，错误、异常链和 traceback frame locals 不含 key 内容或受信 root 外绝对路径

#### Scenario: Runtime 旁路读取被合同拒绝
- **WHEN** contract 静态或运行时检查 shared-budget composition 与 operation identity
- **THEN** fingerprint key 的唯一来源是已验证 `BudgetSettings`，生产代码不通过 `os.environ`、`Path.read_text()` 或自定义 secret-file env 名称读取 key

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
- **WHEN** `base_url` 含 userinfo、query、fragment、非批准 scheme/origin、原始或 percent-encoded dot-segment 越界（含可形成二次编码的结构字符），或 credential 绑定 origin 与 endpoint 不同
- **THEN** startup 返回 `config.invalid`，诊断只包含安全 field path 和 canonical origin 摘要，且在 SDK client、DNS、proxy 或 HTTP 副作用前终止

#### Scenario: Local loopback HTTP 必须双重显式
- **WHEN** local profile 同时显式启用 loopback HTTP 且 endpoint host 是 literal loopback IP
- **THEN** startup 可接受该 origin；相同 URL 在 service profile、使用非 loopback host 或未启用例外时均被拒绝

### Requirement: 流式文本配置具有硬边界且不改变容量合同
typed config SHALL 提供 `model_stream_chunk_utf8_bytes` 与 `model_stream_sensitive_candidate_utf8_bytes`。前者范围 MUST 为 1～4096、默认 1024；后者范围 MUST 为 128～4096、默认 512。版本化注册表中的 64 条 delta 和 65 个 stream 事件容量 MUST 是不可由环境覆盖的硬合同。配置解析 MUST 在 composition 构造 provider 或 invocation 前完成，非法值关闭失败且不得调用 provider。

#### Scenario: 使用合法的小分片配置
- **WHEN** `model_stream_chunk_utf8_bytes=256` 且安全候选上限合法
- **THEN** invocation 以不超过 256 UTF-8 bytes 的目标形成公共分片
- **AND** 仍预留固定 65 个 stream 槽位且最多发布 64 条 delta

#### Scenario: 配置试图扩大硬边界
- **WHEN** 分片大小超过 4096、安全候选上限不在 128～4096，或环境试图配置 delta 数量/stream 容量
- **THEN** 配置解析关闭失败或拒绝未知字段
- **AND** 版本化容量合同不变，provider 未被调用

### Requirement: 默认离线且真实流式验证显式启用
默认配置 SHALL 继续选择 fake provider，并允许 deterministic fake stream 在无网络、无凭证环境覆盖成功、中断、unknown、慢消费和恢复。真实 Pydantic AI 流式验证 MUST 同时要求现有真实 provider opt-in 与独立的流式验证 opt-in；缺失任一条件时测试应明确 skip，而不是失败、伪造成功或读取秘密。

#### Scenario: 默认测试环境
- **WHEN** 未设置真实 provider 和流式验证 opt-in
- **THEN** 流式合同测试使用 fake provider 且不发起网络请求
- **AND** live latency 测试明确 skip

#### Scenario: 仅设置普通真实 provider opt-in
- **WHEN** 只允许一次性真实 provider 调用但未允许流式验证
- **THEN** Phase 18.1 live stream 测试仍明确 skip
- **AND** 不复用一次性结果冒充流式成功

### Requirement: 流式 smoke 输出独立时延证据
opt-in live smoke SHALL 输出 schema `model-stream-live-smoke/v1`，包含 `status`、`provider_called`、`existing_event_first_frame_ms`、`provider_first_delta_ms`、`committed_first_delta_ms`、`client_first_delta_ms` 与 nullable `reason_code`。时延只能是非 bool、非负 integer milliseconds 或 null；smoke MUST 在同一受控进程内协调 local runtime invocation 与事件 client，使 provider、committed 与 client 三项共用首次 provider 迭代前的 monotonic origin，不得跨进程比较不同 monotonic clock。`passed` MUST 具备全部时延、`provider_called=true`、`reason_code=null` 且 provider <= committed <= client；已有事件首 frame独立验证 `<1000ms`，不得解释为 provider SLA。`hosted-unverified` reason 只允许 `authorization_missing|stream_opt_in_missing|credential_missing|endpoint_untrusted`，`failed` reason 只允许 `contract_failure`，`external-blocked` reason 只允许 `network_unavailable|provider_rejected|quota_blocked|provider_timeout|provider_result_unknown`。本地 terminal、capacity、shared-budget、publication、policy、guardrail 或其他编排失败 MUST 输出 `failed`、进程 1/CI fail，并按已观察 response、delta 或稳定错误摘要如实保留 `provider_called`；尤其 `RunOrchestrator.start_run()` 的任何异常都属于独立的本地编排失败事实，即使业务 executor 同时返回 provider-domain 错误且后续 probe/cleanup 成功，也 MUST 最终输出 `failed/contract_failure`。in-process invocation failure MUST 使用封闭 `failure_domain=provider|runtime` 供 executor 区分来源，不得以成功 response 是否存在或通用错误码猜测，这些失败不得伪装为 `external-blocked`。`failure_domain` 不进入 artifact；artifact MUST NOT 包含 prompt、文本、secret、endpoint path、header、response id 或原始异常。

#### Scenario: fake clock 验证成功 artifact
- **WHEN** 默认离线 contract 以 fake clock 驱动 provider 首 delta、event commit 与 client receive 三个边界
- **THEN** artifact 逐字段记录非负 integer milliseconds，且 provider <= committed <= client
- **AND** 单一 total latency 不能替代任一字段

#### Scenario: live 前置不完整
- **WHEN** 本会话授权、stream opt-in、隔离凭据或受信 endpoint 任一缺失
- **THEN** smoke 零 provider 调用，输出 `status=hosted-unverified`、三项 provider 链时延为 null，并映射进程 0/CI skipped

#### Scenario: 获授权后外部阻塞
- **WHEN** 四项前置完整且已授权后发生网络、配额或 provider 故障
- **THEN** smoke 输出 `status=external-blocked`、进程 2/CI fail，并如实记录 `provider_called` 与已知时延
- **AND** 未知时延为 null，任何已知 provider/committed/client 值仍保持单调顺序

#### Scenario: provider 已响应后本地终态失败
- **WHEN** provider response 或 delta 已被观察，但 runtime 在 terminal、capacity、shared-budget 或 publication 边界失败
- **THEN** smoke 输出 `status=failed`、`reason_code=contract_failure`、`provider_called=true` 与进程 1/CI fail
- **AND** 不得把该本地失败降格为 `external-blocked` 或改写为零 provider 调用

#### Scenario: run 启动失败优先于 provider-domain 结果
- **WHEN** `RunOrchestrator.start_run()` 抛出本地异常，业务 executor 另行返回 provider-domain 错误，且 probe 与 cleanup 均成功
- **THEN** smoke 仍输出 `status=failed`、`reason_code=contract_failure` 与进程 1/CI fail
- **AND** `provider_called` 保留 executor 的安全调用事实，不得用 provider-domain 结果覆盖本地编排失败

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
