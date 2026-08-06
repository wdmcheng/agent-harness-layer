# provider-neutral-structured-output Specification

## Purpose
TBD - created by archiving change provider-neutral-structured-output. Update Purpose after archive.
## Requirements
### Requirement: 结构化输出使用稳定且严格的 schema identity
Harness SHALL 以 `output-schema-identity-v1` exact DTO 表达业务输出 schema，exact 字段为 `schema_version="output-schema-identity-v1"`、非空 `schema_ref`、非空 `version` 与 64 位小写 SHA-256 `digest`。Digest MUST 从 strict canonical JSON Schema 的唯一 UTF-8 bytes 计算；canonical 规则为 `ensure_ascii=false`、排序键、紧凑 separators、拒绝 NaN/Infinity，根必须是 object，编译器对未声明extra策略的所有object层递归加入`additionalProperties=false`并拒绝显式放宽为true，不得保留未解析、递归或远程 `$ref`。Schema对象位置的唯一允许关键字为：引用/结构`$defs|$ref|type|properties|patternProperties|additionalProperties|required|items|prefixItems`，约束`enum|const|minimum|exclusiveMinimum|maximum|exclusiveMaximum|multipleOf|minLength|maxLength|pattern|minItems|maxItems|uniqueItems|minProperties|maxProperties|dependentRequired|anyOf|oneOf|allOf|not`，annotation `title|description|default|examples`。位置遍历固定为：只把`$defs/properties/patternProperties`的mapping values、`items/not`的单个object value及`prefixItems/anyOf/oneOf/allOf`的array object items递归解释为schema对象；这些mapping keys与`dependentRequired`属性keys是业务名称，不作关键字检查；annotation、enum/const/default/examples及普通constraint值不递归解释为schema，boolean schema一律拒绝，`additionalProperties`唯一允许值为false。`format|contains|minContains|maxContains|if|then|else|dependentSchemas|propertyNames|unevaluatedProperties|unevaluatedItems`及任何其他schema对象成员必须关闭失败。公共 DTO、Agent descriptor、请求、响应、usage/evidence 与耐久结果不得包含 Python/Pydantic model class、Pydantic AI 或 provider-native schema 类型。

#### Scenario: 相同 schema 重载保持身份
- **WHEN** 同一 Agent version、schema reference 与 canonical JSON Schema 在两个全新 Registry 实例中加载
- **THEN** 两次 descriptor 和 catalog SHALL 产生完全相同的 `schema_ref/version/digest` 与 canonical bytes

#### Scenario: schema 或版本变化产生新身份
- **WHEN** schema 的字段、required、类型、extra policy 或 Agent version 任一变化
- **THEN** 新 identity SHALL 与旧 identity 不同，旧耐久结果不得按 current schema 补齐或解释

#### Scenario: 宽松或不可解析 schema 关闭失败
- **WHEN** schema 显式允许额外字段、含远程/递归/未解析 `$ref`、根不是 object 或含非 JSON 值
- **THEN** Registry 或可信 schema 解析边界 SHALL 在任何 provider 副作用前拒绝，且不得静默放宽或删除约束

#### Scenario: 未支持关键字在编译期拒绝
- **WHEN** 任一schema对象使用`format`、条件/contains/unevaluated系列或不在封闭allowlist中的其他关键字
- **THEN** Registry SHALL 在启动期整体拒绝，不创建FormatChecker或把annotation误当已验证约束

### Requirement: 可信 bound seam 产生 provider-neutral structured result
业务 executor MUST 只通过绑定可信 identity/tenant/run/agent/request/trace 的 `BoundModelInvocationService.complete_structured` 发起结构化调用。该入口 SHALL 只使用当前 Agent Registry 已注册的 output schema，形成 `capability=structured_output` 的legacy非流式单route请求；显式route-chain不属于该成功入口。调用在总reservation、client和provider前 MUST 经过既有`model.invoke` Policy/HITL：DENY以`model.policy_denied`零调用结束；REQUIRE_APPROVAL只返回既有durable waiting信号并冻结原usage/operation/schema/effective-repair identity与同时绑定usage/operation/request/schema/repair的arguments hash，不创建usage claim或provider副作用。批准恢复 MUST 只通过`BoundModelInvocationService.complete_structured_approved`，从durable record/checkpoint恢复上述exact identity、逐值校验active grant/lease与完整arguments绑定，只绕过一次soft gate并重新检查hard route、capability、prompt、当前余额和联合reservation；调用方operation key、current policy或普通text approval identity不得替代durable continuation。成功 `ModelResponse` SHALL 保留 `output_text`，并携带 `structured-output-result-v1` exact DTO：`schema_version="structured-output-result-v1"`、schema identity、`status=valid`、JSON object `value`、非 bool 非负 `repair_count`、正整数 `provider_request_count` 与 64 位小写 replay identity；`output_text` MUST 等于 value 的 canonical JSON。纯文本 response 的 structured result SHALL 为 null。

Structured approval的arguments preimage MUST为`structured-policy-approval-arguments-v1` exact object，exact keys固定为`schema_version/usage_call_id/operation_identity_digest/request/schema_identity/repair_limit`。两个identity MUST为首次bound调用派生的64位小写SHA-256；`request` exact keys固定为`deployment_id/provider/prompt/model/capability/estimated_input_tokens/max_output_tokens/timeout_seconds/route_refs`，所有nullable字段保留JSON null；`route_refs`只能为null或保持原顺序的数组，每项exact keys为`deployment_id/model_id`；capability固定为`structured_output`，token为非bool非负整数，timeout为null或非bool正整数，repair为非bool `0..2` effective limit，schema identity为完整`output-schema-identity-v1`。Arguments hash MUST是该对象的`structured-canonical-json-v1` UTF-8 bytes之小写SHA-256，不得从省略null的DTO payload、provider prompt或current policy派生。批准恢复 SHALL从continuation读取两个identity，再同当前bound request/schema/effective repair重算hash并与grant比较；不得信任调用方operation key重新派生。

Structured approval continuation MUST为`structured-policy-approval-continuation-v1` exact object，exact keys固定为`schema_version/kind/usage_call_id/operation_identity_digest/schema_identity/repair_limit/arguments_hash`；`kind`固定`structured_policy_approval`，三个digest为64位小写SHA-256，usage/operation/schema/repair/hash逐值匹配arguments对象。Approval record MUST在`metadata.continuation`保存完整对象并让`metadata.arguments_hash`等于其hash字段；存在resume token时checkpoint MUST保持`state.kind=agent_executor_approval`并在`state.continuation`保存逐值相同对象；grant hash必须同时等于record、checkpoint与从continuation identity加当前bound request/schema/effective repair重算值。任一extra/missing/type drift，或usage/operation/request/schema/repair/grant/lease、record/checkpoint的单项及组合篡改 MUST在claim、reservation、client和provider前关闭失败。即使record与checkpoint被同步改成同一组合法usage/operation digest，因grant hash仍绑定原identity，也必须拒绝。

Arguments canonical golden vector MUST逐字节成立：`usage_call_id`为64个`1`、`operation_identity_digest`为64个`2`；输入request的nullable deployment/provider/model/timeout/route refs均为null、prompt=`你好`、capability=`structured_output`、estimated/max tokens=`3/8`，schema ref=`example.Output`、version=`1`、digest为64个`0`，repair=`1`；canonical文本为`{"operation_identity_digest":"2222222222222222222222222222222222222222222222222222222222222222","repair_limit":1,"request":{"capability":"structured_output","deployment_id":null,"estimated_input_tokens":3,"max_output_tokens":8,"model":null,"prompt":"你好","provider":null,"route_refs":null,"timeout_seconds":null},"schema_identity":{"digest":"0000000000000000000000000000000000000000000000000000000000000000","schema_ref":"example.Output","schema_version":"output-schema-identity-v1","version":"1"},"schema_version":"structured-policy-approval-arguments-v1","usage_call_id":"1111111111111111111111111111111111111111111111111111111111111111"}`，共643 bytes，SHA-256为`94213e9ecdbbe2e5c50fb565d1ac39462c86e9963c161ba8d2f03b4c5da5efdc`。

#### Scenario: 从公开 bound seam 成功返回
- **WHEN** 支持结构化能力且未声明`fallback_routes`的legacy单route经确定性 provider 返回符合已注册 schema 的 JSON object
- **THEN** response SHALL 同时返回 provider-neutral valid result 与逐字一致的 canonical `output_text`，公共或耐久 payload 不含 SDK/Pydantic AI 类型

#### Scenario: 请求不能覆盖 Agent schema
- **WHEN** 调用方尝试提供另一 schema definition、未知 identity、不同 digest 或比 bound Agent 更高的 repair 上限
- **THEN** 未知schema SHALL使用`model.structured_schema_unknown`，identity/digest冲突 SHALL使用`model.structured_schema_conflict`，repair不是非bool `0..2`或超过Agent/deployment上限 SHALL使用`model.structured_policy_invalid`；三者均在usage claim、reservation、client和provider副作用前结束，且不得创建第二套 schema 控制面

#### Scenario: Policy拒绝保持零调用
- **WHEN** bound identity对`model.invoke`的策略结果为DENY
- **THEN** structured调用 SHALL以`model.policy_denied`在usage claim、reservation、client和send前结束，provider调用数为零且不得把拒绝映射为transport或route失败

#### Scenario: Policy要求审批时只进入durable waiting
- **WHEN** bound identity对`model.invoke`的策略结果为REQUIRE_APPROVAL
- **THEN** structured调用 SHALL返回既有`ModelApprovalRequired`并冻结原usage/operation/schema/effective-repair identity及绑定request/schema/repair的arguments hash，不得创建claim、reservation、prepared handle或provider request

#### Scenario: Durable grant恢复原structured调用
- **WHEN** `complete_structured_approved`收到与durable approval record/checkpoint、active lease、tenant/identity/agent/run/action/resource、request、schema及repair逐值匹配的grant
- **THEN** Harness SHALL恢复原usage/operation identity、只绕过一次soft gate并重新检查hard route、capability、prompt、当前余额与联合reservation后执行；任一字段、lease或current schema漂移均须在provider前拒绝

#### Scenario: Structured approval artifacts与bound input被篡改
- **WHEN** arguments或continuation出现extra/missing/type drift，或usage/operation/request/schema/repair/grant/lease、record/checkpoint的任一单项或组合被篡改，包括只把record与checkpoint两份continuation的usage/operation identity同步改成另一组合法digest且其他字段不变
- **THEN** Harness SHALL以continuation identity加当前bound input重算arguments hash，并同record、checkpoint、grant、active lease及exact continuation逐项交叉校验；因grant hash绑定原usage/operation identity，任一不一致均在usage claim、reservation、client和send前关闭失败

#### Scenario: 文本调用保持兼容
- **WHEN** 既有调用方执行 `complete`、`complete_approved`、`stream` 或 `stream_approved` 的 text capability
- **THEN** 既有签名、route顺序、`output_text`、streaming与durable evidence语义 SHALL 保持不变，structured result为null

### Requirement: 核心 validator 是结构化成功的最终 oracle
Adapter输出进入成功settlement前 MUST 先通过唯一`structured-provider-candidate-v1` exact DTO：字段固定为`schema_version/schema_identity/provider/model/candidate/attempts`；candidate只允许原始JSON字符串或递归JSON object。Candidate不得重复携带顶层token/cost/latency，`attempts` MUST恰含一个`attempt=1`的provider-local `ModelAttemptEvidence`，作为本次single-request send的usage/cost/latency/side-effect/outcome唯一真相源；adapter不得聚合、隐藏或重编号其他transport request。Provider/prepared协议签名固定为async `prepare_structured(request, *, plan, schema)`、`send_structured(*, provider_prompt, repair_ordinal, transport_ordinal)`与`aclose()`；核心为每对`(repair_ordinal,transport_ordinal)`取得fresh prepared call并负责受保护cleanup，prepare不得发送，send最多调用一次且恰好执行一次外部transport request，SDK/client/adapter内部不得重试、退避或repair。`ModelResponse`、裸tuple/object、SDK/Pydantic wrapper或字段漂移一律在核心成功前关闭失败。随后核心 validator MUST 按同一 schema identity 与 canonical JSON Schema 再校验 JSON object。缺字段、类型错误、额外字段、非法 JSON、schema identity 漂移或 canonical value/text 不一致 SHALL 关闭失败；provider-native validation、Pydantic model 或示例断言不得替代该门禁。Invalid value MUST NOT 进入成功 response、业务输出或已通过 eval，原始candidate不得进入repair prompt、异常、evidence、日志或耐久payload。

Structured异常出口 MUST 只使用核心`models/providers.py`定义并公开导出的vendor-neutral类型：`StructuredProviderPrepareError(retryable: bool)`只能在prepare尚未返回prepared call、保证未send且已清理局部资源时抛出；未知prepare异常固定非重试。`StructuredProviderCallError(code, attempts)`只能在send已调用后抛出，code只允许`model.provider_failed|model.provider_side_effect_unknown`且attempts恰含一个`attempt=1`的local事实。两者拒绝unknown字段且不得保存raw message、SDK异常、body/header或secret；adapter私有`ModelProviderError`不得进入核心protocol或doubles。

#### Scenario: 缺字段或类型错误
- **WHEN** provider 返回缺少 required 字段或字段类型不匹配的 object
- **THEN** 结果 SHALL 记为 `model.structured_invalid`，仅在 repair 明确允许时进入下一次同 provider request

#### Scenario: 额外字段一律拒绝
- **WHEN** provider 返回 schema 未声明的顶层或嵌套字段
- **THEN** 结果 SHALL 以 `model.structured_extra_fields` 关闭或进入有界 repair，不得由 Pydantic 默认 extra 策略忽略

#### Scenario: provider 声称成功但 identity 冲突
- **WHEN** adapter未返回exact `StructuredProviderCandidate`，或candidate携带的schema identity/provider/model、JSON类型、sole attempt计量、attempt序列、canonical text或value digest与冻结请求不一致，或额外携带重复顶层token/cost/latency
- **THEN** 核心 SHALL 拒绝成功，保留去敏冲突 evidence，并禁止SDK/裸对象旁路或把冲突 value 耐久化为 valid result

#### Scenario: Candidate只有一个计量真相源
- **WHEN** adapter返回candidate与恰好一个`attempt=1` local attempt，或尝试额外提交顶层token、cost、status、latency
- **THEN** 前者 SHALL 只从sole attempt映射usage/cost/latency与全局ordinal，后者 SHALL 因unknown字段关闭失败；不得在两套计量之间选择或合并

#### Scenario: Structured异常不反向依赖adapter
- **WHEN** prepare在send前失败或send后形成provider失败，而fake、Pydantic AI与第二provider double需要报告同一事实
- **THEN** 三者 SHALL 只使用核心公开`StructuredProviderPrepareError|StructuredProviderCallError`及其exact字段；adapter私有异常、raw message或属性鸭子识别不得进入核心

### Requirement: Repair 受次数、route、token 与 cost 的联合硬边界
结构化 repair limit MUST 是 `0..2` 的非 bool整数，effective limit SHALL 为 deployment上限与请求缩权值的最小值，`provider_request_limit=transport_attempt_limit * (1+effective_repair_limit)`；核心协调器 MUST 显式拥有repair×transport双层循环、单一绝对deadline、backoff、retry分类与次数推进，`transport_attempt_limit`不得解释为adapter内部请求数。每个结构化生成轮次最多使用既有冻结transport attempt上限，任何controller attempt都消耗总上限。每个transport ordinal必须重新prepare；只有`StructuredProviderPrepareError(retryable=true)`允许核心为该ordinal构造`client_prepare_not_started` proof并按冻结backoff/剩余deadline推进，非retryable、未知prepare异常或取消不重试。调用send后无论candidate、封闭call error、HTTP response、timeout、取消或未知异常都计作一个provider request并立即停止transport retry；structured不消费endpoint classifier，classifier继续只服务既有text路径。耐久structured attempts MUST 按`(repair_ordinal,transport_ordinal)`从`(0,1)`字典序连续映射global attempt `1..n`：repair ordinal从0无缺口且不超过effective limit，每个ordinal内transport ordinal从1连续且不超过transport limit；确定终态repair count等于最大repair ordinal，needs-review只有无法证明是否存在未耐久后继attempt时才允许null。

每个返回的fresh prepared call MUST 在核心受保护cleanup中恰调用一次`aclose()`，cleanup不得发送、retry或repair。Send前取消且close成功以`cancelled_before_send` proof、零request、`model.invocation_cancelled` failed收口；send前close失败保留proof/零request但以`model.provider_failed` failed停止。Send后close成功才允许candidate validation/repair或usage完整的call error failed；send已调用后直接逸出的cancel/deadline/未知异常由核心形成一个unknown全局attempt并needs-review。Candidate、call error或该unknown事实之后的close失败一律保留attempt、提升`model.provider_side_effect_unknown` needs-review并保留reservation，不得发布valid、retry或repair。Prepare未返回前由provider负责清理局部资源，不能让核心伪造prepared handle。

每次交给adapter的字符串 MUST 是`structured-provider-prompt-v1` exact object的`structured-canonical-json-v1`文本，exact keys固定为`schema_version/phase/repair_ordinal/business_prompt/schema_identity/schema/validation_codes`。Initial固定`phase=initial`、ordinal 0、空codes；repair固定`phase=repair`、ordinal为`1..effective_limit`且codes为上一轮`validation_issues`投影出的非空、排序去重code字符串，绝不含path、invalid原文、raw异常或secret。Schema必须逐值等于Registry canonical definition，business prompt逐字等于可信请求prompt。`prompt_digest`从initial exact object而非裸业务prompt计算；每个repair prompt由相同initial字段、repair ordinal和上一轮稳定codes唯一复算。

Deployment的`max_prompt_utf8_bytes`在structured capability下约束上述完整provider prompt，不是只约束business prompt。Planning必须在零provider副作用时实际构造initial prompt以及对每个允许repair ordinal使用完整稳定code词汇集合的最大repair prompt，逐个计算UTF-8 bytes；任一超过cap即以`model.input_too_large`零调用拒绝。Structured route的每attempt trusted input bound固定使用`max_prompt_utf8_bytes + catalog.input_envelope_token_bound`而非实际business prompt长度，token/cost联合reservation再乘`provider_request_limit`；因此schema与repair codes全部进入保守上界，任一实际send前仍须断言exact prompt bytes不超过cap。Router/shared budget MUST 用checked arithmetic预约该联合最坏总和。Repair只允许在同一冻结deployment/provider/model内推进；每次实际provider request MUST产生连续attempt、独立token/cost/latency与去敏validation reason，并全部进入调用级usage、budget charge与durable evidence。

#### Scenario: 一次 repair 后成功
- **WHEN** 首次结果 invalid、effective repair limit至少为1且第二次同provider结果valid
- **THEN** response SHALL 显示 `repair_count=1/provider_request_count=2`，两次 actual usage/cost/attempt 全部结算且不调用其他provider

#### Scenario: Repair 次数耗尽
- **WHEN** effective repair limit至少为1，且首次与全部允许repair requests都返回可确定 schema invalid
- **THEN** 调用 SHALL 以 `model.structured_repair_exhausted` 结束，repair count恰为limit、provider request数恰为`1+limit`，已消费actual不得退款或伪装为零；limit为0时首次invalid唯一使用`model.structured_invalid`或`model.structured_extra_fields`，不得伪装成repair exhausted

#### Scenario: 单轮transport上限按耐久ordinal机械拒绝
- **WHEN** structured attempts在任一repair ordinal出现缺失/重复transport ordinal、超过`transport_attempt_limit`、global attempt与字典序不一致，或总count虽未超联合上限但全部挤入单一ordinal
- **THEN** publication/recovery SHALL 关闭失败且不得退款、发布valid或重调provider

#### Scenario: 只有retryable prepare失败推进transport ordinal
- **WHEN** 当前ordinal在prepare尚未返回且未send时抛出`StructuredProviderPrepareError(retryable=true)`，随后下一ordinal可在剩余deadline内启动
- **THEN** 核心 SHALL 为前一ordinal写`client_prepare_not_started` proof与`cleanup_status=not_applicable`，按冻结backoff建立下一fresh prepared；nonretryable、未知prepare异常或取消不得推进

#### Scenario: Send后不消费classifier或transport retry
- **WHEN** prepared send已经调用并产生HTTP response、candidate、call error、timeout、取消或未知异常
- **THEN** 当前ordinal SHALL 计为provider request并成为本轮最后transport ordinal；核心不得读取endpoint classifier或再prepare/send，只有schema invalid且cleanup completed才可进入下一repair ordinal

#### Scenario: Prepared cleanup在send前有唯一终态
- **WHEN** prepared已返回但send前发生取消或deadline，随后`aclose()`正常完成、失败或结果未知
- **THEN** 核心 SHALL 恰调用一次close并分别记录`cleanup_status=completed|failed|unknown`；completed使用`cancelled_before_send`零request cancelled failed，failed/unknown保留相同proof与零request但以provider failed停止，三者均不transport retry

#### Scenario: Prepared cleanup在send后有唯一终态
- **WHEN** send已调用后返回candidate、抛call error、取消或超时，随后`aclose()`正常完成、失败或结果未知
- **THEN** 只有cleanup completed允许candidate validation/repair或usage完整的确定failed；cleanup failed/unknown以及send期间取消/超时 MUST 保留已有或核心unknown attempt并进入needs-review，不发布valid、不retry、不repair、不释放未决reservation

#### Scenario: 总 reservation 不足
- **WHEN** `transport_attempt_limit * (1+repair_limit)` 的最坏token或cost超过Agent/shared hard limit或checked arithmetic溢出
- **THEN** 调用 SHALL 在client、send与provider副作用前以`budget.reservation_rejected`结束，attempt数和provider调用数均为零

#### Scenario: 实际 usage 不完整
- **WHEN** 任一已开始structured request无法提供启用维度的完整actual usage/cost
- **THEN** 调用 SHALL 保留未决reservation并进入unknown/needs-review，不得继续repair或按已知部分伪造最终结算

### Requirement: 结构化终态和 replay identity 必须耐久且 fail closed
`structured-output-replay-v1` SHALL 是拒绝unknown/缺失字段的 exact object，所有nullable字段也必须以JSON null出现。Exact keys固定为`schema_version/tenant_id/run_id/agent_id/request_id/trace_id/usage_call_id/operation_identity_digest/prompt_digest/deployment_id/provider/model/route_digest/schema_identity/transport_attempt_limit/repair_limit/repair_count/provider_request_count/final_status/value_digest`：schema version固定`structured-output-replay-v1`；tenant/run/agent、deployment/provider/model均为非空字符串；request/trace为非空字符串或null；usage call、operation、prompt、route、value摘要均为64位小写SHA-256，其中value digest仅在`final_status=valid`时非null；schema identity为完整`output-schema-identity-v1`；transport limit为正整数，repair limit为非bool `0..2`。确定终态的repair count为非bool `0..2`且不超过limit，provider request count为非bool非负整数且不超过`transport_attempt_limit * (1+repair_limit)`；只有`final_status=needs_review`允许repair count和provider request count分别为上述exact整数或JSON null，null唯一表示该维度无法从耐久事实证明，已知维度不得因另一维unknown而省略。Final status只允许`valid|invalid|extra_fields|repair_exhausted|failed|needs_review`。Valid必须有value digest且provider request count为正；invalid/extra/exhausted必须有正request count；failed允许request count为0或正数；所有非valid状态value digest必须为null。

Replay、operation与prompt摘要统一使用`structured-canonical-json-v1`：只接受JSON object/array/string/bool/non-bool integer/有限float/null，拒绝Decimal、bytes、NaN/Infinity与非字符串object key；UTF-8、`ensure_ascii=false`、`sort_keys=true`、紧凑separators `(',', ':')`、`allow_nan=false`，array保持顺序，null保留JSON null，所有exact字段不得省略或默认补齐。`operation_identity_digest`从exact object`{schema_version="structured-output-operation-v1",tenant_id,run_id,agent_id,request_id,trace_id,operation_key}`计算，operation key只存在于瞬时preimage；`prompt_digest`从前述initial `structured-provider-prompt-v1`完整canonical bytes计算；`route_digest`从既有脱敏`route_plan_identity_payload(plan)`按同一serializer计算；`value_digest`从valid value的canonical bytes计算。公共或耐久payload只保存digest，不保存operation key、prompt、schema definition、secret、endpoint、raw error或SDK对象。实现与validator MUST 逐字节产生以下golden vector：

```text
{"agent_id":"agent-a","deployment_id":"deployment-a","final_status":"valid","model":"model-a","operation_identity_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","prompt_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","provider":"provider-a","provider_request_count":2,"repair_count":1,"repair_limit":1,"request_id":null,"route_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","run_id":"run-a","schema_identity":{"digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","schema_ref":"agents.example.schemas:Output","schema_version":"output-schema-identity-v1","version":"1.0.0"},"schema_version":"structured-output-replay-v1","tenant_id":"tenant-a","trace_id":"trace-a","transport_attempt_limit":1,"usage_call_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","value_digest":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}
sha256=6c3ef8f5d9444b1e70796996344af02033b29810b7fb6537b483e8ea230ff819
```

只有schema、未声明`fallback_routes`的legacy单route和预算均冻结并创建durable started claim后，final/needs-review才生成非null replay identity。未知/冲突schema、capability unsupported、任意显式route-chain和budget rejection等零调用preflight终态使用稳定error/status、`provider_request_count=0`和`replay_identity=null`，不得伪造不存在的schema/replay identity或创建usage claim；replay conflict校验既有耐久identity后零调用拒绝且不覆盖旧结果。已耐久 success/failure只允许exact replay。Claim建立后的可证明prepare发送前失败、每个transport attempt均有核心发送前`client_prepare_not_started`证明的retry耗尽、close成功且具备完整终态/usage的确定provider failure，或durable mark提交状态确定且send前取消/close成功，使用同一冻结preimage与`final_status=failed`生成非null identity；无法证明结果、usage、mark commit ack、send后取消/timeout/未知异常、send后cleanup或repair/request基数时，以`final_status=needs_review`、null value digest与逐维度exact-or-null计数围栏。Mark commit ack未知但send仍可证明未调用时，保存`provider_request_count=0`与`cancelled_before_send` proof，但error/status固定`model.provider_side_effect_unknown`/needs-review，actual token/cost为null且direct/allocation reservation与owner ledger不释放。尤其durable started已提交但send前后边界未形成耐久attempt事实的crash，repair/request count按可证明事实分别为0或null，不得伪造正request count。任何到达send或收到HTTP response的structured legacy attempt均已计为provider request并停止transport retry，不得因endpoint-bound classifier或状态码推进、改写为not-started/actual-zero；该attempt启用的usage/cost不完整时立即needs-review并保留reservation。

#### Scenario: Claim后的确定transport或provider失败可精确重放
- **WHEN** durable started claim已建立，随后client/prepare确定发送前失败、当前ordinal每个transport attempt均以`client_prepare_not_started`发送前proof耗尽、provider返回确定失败且usage完整，或取消可证明没有未决副作用
- **THEN** Harness SHALL 以`final_status=failed`和`model.provider_failed|model.provider_retry_exhausted|model.invocation_cancelled`中唯一匹配的既有稳定错误耐久化非null replay identity；provider request count按实际已到达send边界的request计为0或正数，exact replay不重调provider

#### Scenario: Exact success replay只补投
- **WHEN** 同一语义请求和operation slot重放已耐久valid result
- **THEN** Harness SHALL 完整校验response/evidence/schema/replay/attempt/charge后返回同一结果或补投final，provider调用次数保持不变

#### Scenario: 语义冲突 replay 零调用拒绝
- **WHEN** 同一operation slot改用不同schema identity、repair policy、prompt语义或route identity
- **THEN** Harness SHALL 以`usage.settlement_replay_blocked`或`model.structured_replay_conflict`拒绝，且不得调用provider、覆盖旧result或释放旧reservation

#### Scenario: Started crash 恢复进入 needs-review
- **WHEN** durable started已提交但没有可验证final result/usage，进程随后恢复
- **THEN** recovery SHALL 提升或保持unknown/needs-review，repair/provider request count各自按耐久事实使用exact非负整数或null，禁止用0/1伪造unknown，也禁止自动重发structured request、repair或切换provider

#### Scenario: Durable payload 篡改关闭失败
- **WHEN** 持久化structured response、schema/replay identity、attempt、usage或budget charge任一缺失、多余、冲突或被同步篡改
- **THEN** public replay与后台recovery SHALL 在发布final/telemetry或标记published前共同拒绝，不返回成功也不重调provider

### Requirement: Phase 19 不授权 structured streaming、跨 provider fallback 或工具执行
结构化调用 MUST 只使用legacy非流式单route。Agent policy只要显式声明任意非空`fallback_routes`就保持Phase18.2 route-chain identity；无论原始列表含一个还是多个route，也无论request是否缩权到一个candidate，Harness SHALL 在usage claim、reservation、attempt、client和provider副作用前以`model.structured_route_not_allowed`结束，不删除、重排、试探或把显式chain降级为legacy。Deployment capability或provider protocol任一不支持时，`_invocation_structured.py` SHALL在usage claim、reservation、client和provider副作用前统一以`model.structured_capability_unsupported`结束；底层Router的通用`model.capability_unsupported`不得从公开structured seam逸出。Harness不得把text completion切片、fake或后继provider作为后备。结构化value与tool intent MUST 使用不同判别类型；任何字段看起来像tool都不得触发工具解析或执行。

#### Scenario: 任意显式 structured route-chain 被拒绝
- **WHEN** bound Agent声明一个或以上`fallback_routes`并发起structured调用，包括request缩权后只剩一个candidate
- **THEN** 调用 SHALL 在usage claim/reservation/attempt/client/send前拒绝，所有候选provider调用数为零、原顺序不被改写且显式chain不降级为legacy

#### Scenario: Provider不支持结构化能力
- **WHEN** deployment未声明`structured_output`或绑定provider未实现structured protocol
- **THEN** 调用 SHALL 以稳定unsupported终态结束，不调用text seam、不切fake或后继provider

#### Scenario: 类工具JSON不执行工具
- **WHEN** valid或invalid JSON包含`tool`、`arguments`或类似字段
- **THEN** 本次模型调用自身 SHALL仍按相同顺序且恰好一次经过`model.invoke` Policy/HITL；只有在该调用级门禁完成后，类工具字段才按业务schema验证并返回/拒绝，不进入ToolRegistry、不触发工具调用专属Policy/HITL或任何工具副作用

#### Scenario: 类工具字段不改变调用级Policy终态
- **WHEN** 相同bound request仅改变JSON候选是否含`tool`或`arguments`字段，而调用级Policy分别返回DENY或REQUIRE_APPROVAL
- **THEN** DENY SHALL始终零claim、零reservation、零send，REQUIRE_APPROVAL SHALL始终进入同一exact durable waiting；内容字段不得增加、跳过或重排调用级Policy决策

### Requirement: 工具意图与结构化业务输出使用不同判别合同
Provider-neutral structured output SHALL 继续只表示 Agent 注册输出 schema 下的最终业务结果。工具意图 SHALL 使用独立 `ProviderToolIntentCandidate`、`ToolIntent`、capability、schema identity、错误码和 turn discriminator；系统 MUST NOT 用 structured result 的任意字段、schema 名称或 JSON 内容触发工具 resolve/执行，也 MUST NOT 把工具 intent 当业务成功结果发布。

#### Scenario: 结构化结果包含工具形状仍是业务结果
- **WHEN** Agent output schema 合法允许 `tool_name` 或 `arguments` 字段且 provider 返回匹配 value
- **THEN** 结果继续是 `final_structured` 并按 MOD-005 验证
- **AND** ToolRegistry 与所有 handler 调用计数为零

#### Scenario: 工具候选不能进入业务 structured settlement
- **WHEN** provider 返回 `ProviderToolIntentCandidate`
- **THEN** 核心只能按 tool-intent capability 验证
- **AND** 不生成 MOD-005 valid structured result 或业务 eval success
