## Context

Phase 18/18.1/18.2 已生产化 typed deployment、provider-neutral `ModelRequest/ModelResponse`、可信 bound invocation、shared-budget reservation、durable usage outbox、exact replay/recovery 与安全 route-chain。当前结构化缺口集中在四处：Registry 只验证后丢弃 Python schema class；descriptor 只有未版本化 dotted ref；adapter 把任意 `result.output` 直接 `str()`；semantic request、route reservation和durable response没有schema/repair identity。

本 change 跨 Registry、config、router/invocation、adapter、budget/evidence/recovery，因此使用一个单一纵向 change 和单一写 owner。`Design-Brief.md`、设计稿、HTTP/SSE UI均不适用。ADR-0002要求vendor import只留在adapter；Phase18.2要求不得改写text route-chain顺序。

本机调查确认锁定的Pydantic AI 2.5.0支持per-run output type，但SDK默认structured策略可能采用tool-output语义并隐藏内部validation retry。Phase19禁止tool call/工具循环，且每次实际provider request都必须进入Harness usage/budget/evidence，因此不把SDK自动structured retry当核心控制器。

## Goals / Non-Goals

**Goals:**

- 用可版本化、可复算、严格extra policy的provider-neutral schema identity绑定Agent授权、请求、route、result、usage与replay。
- 从`BoundModelInvocationService.complete_structured`完成未声明`fallback_routes`的legacy单route、非流式、有限repair生产调用；所有transport/repair attempts进入同一reservation和settlement。
- 让valid、invalid、unsupported、budget、exhausted、replay conflict与unknown/needs-review都可耐久恢复且fail closed。
- 保留`ModelResponse.output_text`和所有text-only/non-stream/stream/route-chain行为。

**Non-Goals:**

- structured streaming、跨provider structured fallback、tool call/工具循环/执行。
- provider-native DTO或Pydantic AI output type成为核心协议。
- fake隐式后备、真实provider调用、Phase20/21或UI工作。
- OpenSpec sync/archive、commit、push、release、deploy。

## Decisions

### 1. Registry编译并持有`OutputSchemaDefinition`

在新的核心`models/structured.py`定义：

- `OutputSchemaIdentity(schema_version, schema_ref, version, digest)`；
- `OutputSchemaDefinition(identity, json_schema)`；
- `StructuredOutputRequest(schema, repair_limit)`；
- `StructuredProviderCandidate(schema_version, schema_identity, provider, model, candidate, attempts)`；
- `StructuredOutputResult(schema_version, schema, status, value, repair_count, provider_request_count, replay_identity)`；
- validation/replay canonical helpers与稳定异常。

`StructuredProviderCandidate`是adapter到核心validator之间唯一的未验证候选DTO，不进入Agent descriptor、HTTP API或耐久payload。它是`extra=forbid/frozen`的exact object：`schema_version`固定`structured-provider-candidate-v1`；`schema_identity`必须逐值回显冻结identity；`provider/model`必须逐值匹配route plan；`candidate`只允许原始JSON字符串或`dict[str, StructuredJsonValue]`，其中递归JSON值只含null/bool/int/有限float/string/list/object，根list/scalar和SDK/Pydantic对象均拒绝；`attempts`必须恰含一个provider-local `ModelAttemptEvidence`，其`attempt=1`且只描述本次single-request send，不能聚合、隐藏或重编号其他transport request。Candidate不再重复携带顶层token、cost或latency；唯一local attempt是本次请求usage、cost、latency、side effect与outcome的单一真相源。原始candidate只在当前进程交给核心解析/验证，不进入repair prompt、异常、evidence或日志；核心把该local attempt与当前schema/prompt/repair/transport ordinal合成全局连续`StructuredModelAttemptEvidence`。成功候选若sole attempt的usage/cost事实不完整也不得发布valid，只能进入既有unknown/needs-review围栏。

核心在`models/providers.py`定义并从`models/__init__.py`公开导出两种vendor-neutral异常，adapter私有`ModelProviderError`不进入structured协议：`StructuredProviderPrepareError(retryable: bool)`只能由尚未返回prepared call、保证未send且已清理本地资源的prepare边界抛出，`retryable`必须是真bool；其他prepare异常一律非重试确定失败。`StructuredProviderCallError(code, attempts)`只能在send已调用后抛出，`code`只允许`model.provider_failed|model.provider_side_effect_unknown`，`attempts`必须恰含一个`attempt=1`的provider-local事实。异常对象不保存raw message、SDK异常、response body/header或secret；既有text adapter可继续兼容re-export和使用其私有错误，不反向依赖新structured类型。

`AgentRegistry.load_from_directory()`在已有受控import context中取得BaseModel class并调用`model_json_schema()`，随后通过唯一compiler：

1. 深复制JSON-compatible schema；
2. 只接受根object；解析并内联本地非递归`$defs/$ref`，拒绝remote/recursive/unresolved ref；
3. 按schema对象位置执行封闭关键字allowlist：`properties/$defs/patternProperties`的成员名与`dependentRequired`的属性名不当作关键字，其他schema对象成员必须命中主capability spec列出的结构、约束或annotation集合；`format`、条件、contains与unevaluated系列当前关闭失败；
4. 对每个object缺省加入`additionalProperties=false`，显式`true`直接拒绝；
5. 用Draft 2020-12 meta-schema检查；
6. 以统一canonical JSON bytes计算digest。

Schema version使用Agent descriptor version，避免增加第二个业务版本字段；ref继续沿用已有dotted reference。`AgentDescriptor`保留既有`input_schema_ref/output_schema_ref`兼容字段并新增identity，不携带definition/class。Registry构造器接受可选provider-neutral definitions，便于测试注入；磁盘loader必须原子加载descriptor/schema/executor后再构造索引。

选择直接声明`jsonschema>=4.26.0,<4.27`为核心runtime依赖：当前lock和合规清单已有4.26.0，窄minor范围配合不读取message/context的Harness归一化避免依赖transitive偶然性和错误树漂移，Draft validator也比自写部分JSON Schema解释器更安全。备选是Pydantic `TypeAdapter`或自写validator；前者会把业务Python type带入runtime/replay，后者难以正确覆盖nested object/ref/number语义，均拒绝。

### 2. Bound façade是唯一业务structured authority

`ModelInvocationService`接收窄`output_schema_resolver(agent_id) -> OutputSchemaDefinition`，composition传入Registry只读方法。`BoundModelInvocationService.complete_structured(request, operation_key, repair_limit=0)`只接受普通prompt/token/route缩权字段，使用context绑定agent解析schema，并构造`capability=structured_output`及`StructuredOutputRequest`。Policy允许时进入既有总reservation与provider路径；DENY以`model.policy_denied`零调用结束；REQUIRE_APPROVAL抛出既有`ModelApprovalRequired`，且不得创建usage claim、reservation、client或send。

Structured审批绑定使用两份版本化exact对象。`structured-policy-approval-arguments-v1`的exact keys为`schema_version/usage_call_id/operation_identity_digest/request/schema_identity/repair_limit`：`schema_version`固定为同名v1；两个identity均为首次bound调用已派生的64位小写SHA-256；`request`必须精确保留`deployment_id/provider/prompt/model/capability/estimated_input_tokens/max_output_tokens/timeout_seconds/route_refs`九个keys，nullable字段保留JSON null，`route_refs`只能为null或保持原顺序的数组，每项exact keys为`deployment_id/model_id`；`capability`固定`structured_output`，两个token字段为非bool非负整数，timeout为null或非bool正整数，repair为非bool `0..2`且必须等于planning得到的effective repair limit；`schema_identity`为完整`output-schema-identity-v1`。该对象使用`structured-canonical-json-v1`生成UTF-8 bytes并取小写SHA-256，作为唯一`arguments_hash`，不得使用`HarnessDTO.to_payload(exclude_none=true)`或provider prompt替代。批准恢复不从调用方operation key重派生identity，而是从continuation取得两个identity并同当前bound request/schema/repair重算该hash；grant hash因此是record/checkpoint之外的独立identity锚点。

`structured-policy-approval-continuation-v1`的exact keys为`schema_version/kind/usage_call_id/operation_identity_digest/schema_identity/repair_limit/arguments_hash`：版本固定为同名v1，`kind=structured_policy_approval`，两个identity与arguments hash均为64位小写SHA-256，schema identity、repair limit、usage identity和operation identity逐值等于arguments preimage。Approval record的`metadata.continuation`保存该完整对象且`metadata.arguments_hash`等于其hash字段；存在resume token时，checkpoint保持既有`state.kind=agent_executor_approval`并在`state.continuation`保存逐值相同对象。Grant的`arguments_hash`必须同时等于record metadata、checkpoint continuation和从continuation两个identity加当前bound request/schema/effective repair重新计算的hash；tenant/identity/agent/run/action/resource仍按既有grant合同逐值校验。同步改写record/checkpoint中的任一identity会改变重算hash，而grant hash不可由两份continuation改写，因此必须关闭失败。

机械golden vector固定为：`usage_call_id`为64个`1`、`operation_identity_digest`为64个`2`；普通request的nullable deployment/provider/model/timeout/route refs均为null、prompt为`你好`、capability为`structured_output`、estimated/max tokens为`3/8`，schema为`example.Output` version `1`和64个`0`digest，repair为1。Canonical UTF-8文本为`{"operation_identity_digest":"2222222222222222222222222222222222222222222222222222222222222222","repair_limit":1,"request":{"capability":"structured_output","deployment_id":null,"estimated_input_tokens":3,"max_output_tokens":8,"model":null,"prompt":"你好","provider":null,"route_refs":null,"timeout_seconds":null},"schema_identity":{"digest":"0000000000000000000000000000000000000000000000000000000000000000","schema_ref":"example.Output","schema_version":"output-schema-identity-v1","version":"1"},"schema_version":"structured-policy-approval-arguments-v1","usage_call_id":"1111111111111111111111111111111111111111111111111111111111111111"}`，共643 bytes，SHA-256为`94213e9ecdbbe2e5c50fb565d1ac39462c86e9963c161ba8d2f03b4c5da5efdc`。

`BoundModelInvocationService.complete_structured_approved(request, operation_key, repair_limit, grant)`是唯一批准恢复入口。`operation_key`只为签名兼容且不得重新派生identity；服务必须从durable approval record与checkpoint逐值恢复上述exact continuation，拒绝任一extra/missing/type drift，校验active lease、tenant/identity/agent/run/action/resource，并用continuation的usage/operation identity加当前bound request/schema/repair重算arguments hash，与record/checkpoint/grant逐值比较，再确认current Registry schema identity与冻结值相同。Record与checkpoint任一缺失或不一致、二者的usage/operation identity被同步篡改、或grant/lease/request/schema/repair的任一单项或组合漂移，均须在provider前关闭失败。通过后只绕过一次soft policy gate，复用原`usage_call_id/operation_identity_digest`并重新执行hard route、capability、prompt、当前余额和联合reservation检查。不得把普通`complete_approved`的text identity、调用方operation key或current policy作为恢复真相源。

structured纵向控制器按有效代码行和单一职责拆为五个私有owner：`models/_invocation_structured.py`只保留schema resolver、hard preflight、Policy/HITL、replay入口与组件编排；`_invocation_structured_support.py`承载route/budget/attempt摘要、bounded cleanup与started recovery辅助；`_invocation_structured_execution.py`独占transport×repair循环及prompt/validation；`_invocation_structured_result.py`独占provider-neutral result/evidence/replay与最终settlement投影；`_invocation_approval_identity.py`独占approval arguments/continuation及durable恢复校验。`invocation.py`只保留resolver注入、`complete_structured/complete_structured_approved`公共bound façade与窄委托；`storage/_structured_usage_evidence_repository.py`只复用既有UoW session绑定started replay seed，公共repository入口和事务边界不变。`models/structured.py`只保留公共DTO、canonical/replay/prompt纯逻辑；`structured_schema.py`独占strict compiler、JSON Schema validator与稳定issue投影。所有新模块禁止取得额外公共出口或改变send/claim/reservation/cleanup顺序，每个交付manifest内Python生产文件最终有效代码行必须不超过500；这是已有代码的行为等价职责拆分，不进入Phase21 service locator、storage port或状态机重构。

有效代码行门禁必须直接解析living plan中本次changed-file manifest的完整“生产路径”代码块，逐一检查其中全部Python文件；禁止维护手写的structured或阶段文件白名单。可信内部协作者同样属于冻结类型边界：共享预算为`IdentityRuntime | None`，Policy resolver为`Callable[[str], AgentModelPolicy] | None`，执行settlement为`SettlementStart`，prepared handle为`PreparedStructuredModelCall`，prompt builder为参数完整的`StructuredPromptBuilder` Protocol；这些位置不得使用对象级`Any`或`Callable[..., str]`。合同测试用AST逐项锁定上述注解并验证manifest可解析，从而在Pyright之外防止宽类型和漏扫回归。

Schema identity和repair policy进入`_semantic_request()`及route operation identity；durable replay先比较旧identity，不能在current Registry上重新解释旧result。Unknown/mismatch在route plan后、provider准备前通过既有失败settlement保存零调用evidence；已有durable settlement仍优先按原identity验证。

备选是让请求携带任意JSON Schema或把Pydantic model传给bound method；两者都会扩大Agent descriptor授权并泄漏Python类型，因此拒绝。

### 3. Structured route复用单route顺序并显式拒绝chain

Typed deployment capabilities扩展`structured_output`，新增`max_structured_repair_attempts: 0..2`。Current/snapshot router把schema identity、effective repair limit、`transport_attempt_limit`、联合`provider_request_limit`与token/cost bounds冻结到`ModelRoutePlan`和route evidence。

若Agent policy含非空`fallback_routes`或planning生成`ModelRouteChainPlan`，structured调用在reservation/attempt/permit/client前返回`model.structured_route_not_allowed`。不借request删除候选来伪造单route，也不改Phase18.2 chain planner。Text capability完全走旧路径。

Structured单route继续使用legacy顺序：hard eligibility/schema/capability → Policy/HITL →总reservation → Bulkhead permit → lazy client → durable started → send。批准恢复只跳过已由durable grant证明的soft gate，hard bounds与当前余额必须重算。不得为了结构化输出改成Phase18.2 chain的attempt-start-before-client顺序。

### 4. Harness拥有transport与repair联合attempt控制器

`ModelStructuredProvider`/`PreparedStructuredModelCall`是provider-neutral runtime-checkable protocol，签名唯一冻结为：`prepare_structured(request: ModelRequest, *, plan: ModelRoutePlan, schema: OutputSchemaDefinition) -> PreparedStructuredModelCall`；prepared call的`send_structured(*, provider_prompt: str, repair_ordinal: int, transport_ordinal: int) -> StructuredProviderCandidate`与`aclose() -> None`均为async。`repair_ordinal`是非bool `0..repair_limit`，`transport_ordinal`是非bool `1..transport_attempt_limit`，`provider_prompt`必须是当前repair ordinal完整`structured-provider-prompt-v1` canonical文本。核心为每一对`(repair_ordinal, transport_ordinal)`取得一个fresh prepared call，并在finally中恰好关闭一次；prepare发生在该attempt的send前且不得发送，prepared call最多调用一次send，send恰好执行一次外部transport request且不得在SDK、client或adapter内部重试、退避或repair。Router/协调器在reservation/started前验证provider显式实现；fake和Pydantic AI adapter分别实现，普通`ModelProvider.complete`保持不变。Adapter不得返回`ModelResponse`、裸tuple/object或SDK wrapper绕过该DTO。

核心协调器显式拥有transport与repair双层循环、单一绝对deadline、backoff、retry分类和次数推进；`transport_attempt_limit`表示每个repair ordinal内最多由核心启动的attempt数，不是adapter内部请求数。一个结构化生成轮次最多执行冻结`transport_attempt_limit`次transport attempt；schema invalid才进入下一repair轮次，总轮次`1+repair_limit`，联合硬上限为二者乘积。所有actual或发送前已证明attempt全局从1连续：

- claim后每个transport ordinal都重新prepare；只有`StructuredProviderPrepareError(retryable=true)`允许核心在该控制流点为该ordinal构造`client_prepare_not_started` proof、按冻结backoff与剩余deadline推进下一transport ordinal，因此零request retry exhausted可机械具有恰好`transport_attempt_limit`个独立proof。非retryable或未知prepare异常以`model.provider_failed`确定停止；prepare期间或send前取消在durable mark提交状态已确定时以`cancelled_before_send` proof和`model.invocation_cancelled`停止，不重试；若mark commit ack未知，即使send仍可证明未调用，也以`model.provider_side_effect_unknown`/`needs_review`保留direct/allocation reservation与owner ledger围栏；
- 一旦调用send，无论candidate、`StructuredProviderCallError`、HTTP response、timeout、取消或未知异常，该attempt都计作一个provider request且当前调用不再transport retry。Endpoint-bound classifier只继续服务既有text路径，structured绝不因HTTP response/header/status推进下一ordinal；usage/cost完整的确定call error可以failed收口，不完整、unknown、timeout或取消一律立即needs-review；
- adapter返回candidate或`StructuredProviderCallError`时只提供一个local attempt，核心把它映射为全局/repair/transport ordinal。若send已调用后直接逸出`CancelledError`、deadline或未封闭异常，核心构造一个同ordinal的unknown全局attempt作为唯一保守替代，不伪造candidate/local DTO，并立即needs-review；
- provider返回文本/JSON后，adapter只归一化JSON-compatible候选与原始文本，不宣布业务valid；
- 核心validator校验。Initial/repair都把`structured-provider-prompt-v1` exact object的canonical JSON文本作为唯一provider prompt；initial含业务prompt、完整schema、空codes和ordinal0，repair只增加ordinal及上一轮排序去重codes，不回传path、invalid原文、raw异常或secret；
- structured planning按完整prompt cap为每attempt预约，且在零调用时实际构造initial与各ordinal最大code集合prompt并检查UTF-8 bytes；schema/codes不允许落在预算外。Input/output price values与price source ref/version分别成对完整；启用price必须绑定完整source，cost关闭允许value/bound为null而完整catalog/source identity继续进入route/evidence，半pair统一在公开structured seam以`budget.reservation_rejected`零副作用拒绝；
- 每个fresh prepared call都在受保护cleanup中恰好调用一次`aclose()`，cleanup不发送、不授权retry/repair，也不覆盖已形成的attempt事实。Send前取消且close成功使用`cancelled_before_send` proof、request count 0与`model.invocation_cancelled` failed；send前close失败仍保留proof和零request，但以`model.provider_failed` failed停止。Send后只有close成功才允许既有candidate继续validation/repair或完整call error确定failed；candidate后、call error后或send期间取消/timeout/未知之后的close失败一律保留已有或核心unknown attempt、提升`model.provider_side_effect_unknown` needs-review、保留reservation且不发布valid、retry或repair。Prepare尚未返回就失败/取消时由provider自行清理其局部资源，核心没有伪造prepared handle；违反该协议按非重试failed关闭。
- valid立即收口；repair limit至少为1且所有repair用尽才返回`model.structured_repair_exhausted`；limit0的首次失败唯一返回invalid/extra。Claim后的可证明prepare发送前失败、当前ordinal所有transport attempt均有`client_prepare_not_started`核心发送前proof的retry耗尽、usage完整且close成功的确定provider failure，或send前取消且close成功，以`failed`和既有稳定错误收口；任一结果/usage/send后取消或cleanup副作用unknown立即停止并needs-review。

Pydantic AI adapter继续调用`Agent.run(..., retries=0)`取得普通字符串/JSON-compatible output，并把底层transport client配置为single-attempt，不使用`ToolOutput`、SDK内部output retry或transport retry；这样每次SDK调用与一个Harness transport ordinal逐值对应。若output不是字符串或JSON-compatible object，adapter转换为封闭provider failure/invalid事实，不`str()`任意SDK对象。既有text adapter的retry语义不变。备选使用`NativeOutput/StructuredDict`；它可优化部分provider，但会让capability、内部retry/usage和tool-output模式依赖SDK profile，Phase19暂不采用。未来可在adapter内作为不改变公共协议的独立优化change。

Structured preflight错误只允许一套公开映射：未知schema使用`model.structured_schema_unknown`，identity冲突使用`model.structured_schema_conflict`，请求repair不是非bool `0..2`或超过Agent/deployment上限使用`model.structured_policy_invalid`，任意显式`fallback_routes`使用`model.structured_route_not_allowed`，完整prompt超限使用`model.input_too_large`，预算不足使用`budget.reservation_rejected`。Deployment缺少`structured_output`或provider未实现上述protocol都必须在usage claim/reservation/client前由`_invocation_structured.py`统一返回`model.structured_capability_unsupported`；底层current/snapshot Router为通用能力检查产生的`model.capability_unsupported`不得从公开structured seam逸出。配置本身非法仍只在启动期使用`config.invalid`。

### 5. 核心validator、canonical text与replay identity唯一实现

Core parser只接受单个JSON object；string使用严格`json.loads`，object先验证JSON-compatible，禁止bytes、class、Decimal/NaN等。`jsonschema.Draft202012Validator`按Registry definition验证，只消费`iter_errors()`直接返回项，绝不递归`ValidationError.context`，也不启用`FormatChecker`；validation errors只投影稳定codes/path摘要，不保存raw output或异常。Path按absolute path和RFC6901 escaping构造；required/extra从validator声明与instance keys集合运算展开，绝不解析不稳定message；支持keyword集合、issue上限/overflow折叠和混合错误优先级由usage delta冻结。首次/limit0只要存在extra即唯一使用`model.structured_extra_fields`，否则`model.structured_invalid`；repair耗尽统一提升为`model.structured_repair_exhausted`。

Valid value重新按`structured-canonical-json-v1`序列化，结果同时写`StructuredOutputResult.value`与`ModelResponse.output_text`。该serializer只接受JSON-compatible值并固定UTF-8、Unicode、键序、紧凑分隔符、有限number与显式null；schema、operation、prompt、route、value和replay摘要只允许这一实现。

Replay identity以`structured-output-replay-v1` exact object计算，字段固定为版本、tenant/run/agent、nullable request/trace、usage call、operation/prompt digest、deployment/provider/model、route digest、完整schema identity、transport/repair policy、逐维度exact-or-null的repair/request count、final status和nullable value digest。Operation/prompt瞬时preimage、所有required/null规则、终态union、golden bytes与SHA-256以主capability spec为唯一实现契约；公开和耐久payload只保存digest。零调用preflight rejection没有已冻结完整preimage，固定null replay且不创建usage claim；durable started后的确定失败与needs-review分别用冻结输入及`final_status=failed|needs_review`生成非null identity。只有needs-review允许count为null，表示该维度的真实基数无法由耐久attempt事实证明。

### 6. 复用现有JSON耐久列，不新增migration

现有evidence outbox/shared-budget result以JSON保存完整`ModelResponse`和`ModelUsageEvidence`，新增DTO字段可由现有列承载。`_durable_response()`、settlement validator、replay seed和publication validator扩展exact cross-validation；无需新表、索引或migration。

SQLite与PostgreSQL仍需要合同：使用现有schema分别验证structured success/exhausted/failed/unknown的写入、exact replay与篡改拒绝，而不是因为“无migration”跳过数据库证据。若red contract证明现有列或状态无法表达identity/failed/needs-review，必须暂停实现、更新proposal/design/tasks和矩阵owner，再重新strict及契约`1+2`；不得实现中偷加migration。

### 7. Evidence把structured摘要与attempt/budget交叉绑定

`ModelUsageEvidence.decision.structured_output`使用`structured-output-evidence-v1` exact DTO，字段固定为版本、完整schema identity、封闭status、repair/provider request limit与count、nullable replay identity、有序`validation_issues[{code,path}]`和nullable error code。Started/valid/invalid/extra/exhausted/failed/needs-review的required/null组合、稳定code映射、RFC 6901 path、排序与去重由`model-usage-evidence`delta逐值冻结；preflight rejection不伪造decision摘要。结构化`ModelResponse.attempts`使用`StructuredModelAttemptEvidence`判别子型：在文本attempt基字段之外持有schema/prompt identity、repair/transport ordinal、repair trigger/validation codes、可复算not-started proof与`not_applicable|completed|failed|unknown` cleanup status；文本attempt形状不变。Provider request count按已到达send边界、即没有核心发送前proof的attempt精确计算，unknown时为null且attempts作为已知下界；send后attempt始终计数且停止retry，启用的usage/cost不完整或cleanup failed/unknown使整个调用needs-review并保留reservation。Settlement按全部已知attempt聚合并围栏未知部分，不新增第二套charge数组。

Validator强制：

- valid必须有structured result、canonical text、replay identity且error为空；
- invalid/exhausted不得有valid response/value；
- `provider_request_count`与attempt观察事实一致，`repair_count <= repair_limit`；
- structured total charge不超过冻结联合reservation；
- unknown/needs-review不得有valid结果或actual总额；
- text evidence不得出现structured摘要。

## Affected Surfaces

**生产核心**

- `packages/agent-harness/pyproject.toml`、`uv.lock`、已有`compliance/third-party.toml`身份复核。
- `config/schemas.py`及current/snapshot route解析。
- `registry/__init__.py`、`registry/descriptor.py`、`registry/_loader.py`、`registry/registry.py`。
- `models/structured.py`（公共DTO/canonical/replay/prompt纯逻辑）、`structured_schema.py`（strict compiler/validator）、`_invocation_structured.py`（入口/preflight/Policy/编排）、`_invocation_structured_support.py`（planning/attempt/recovery辅助）、`_invocation_structured_execution.py`（transport×repair执行）、`_invocation_structured_result.py`（result/evidence/replay/final settlement投影）、`_invocation_approval_identity.py`（exact structured approval continuation恢复）、`providers.py`、`usage.py`、`router.py`、`invocation.py`（只加公共bound façade、structured approved façade与窄委托）、`_invocation_execution.py`、`_invocation_planning.py`、`_invocation_settlement.py`、`_invocation_evidence.py`、`_invocation_chain_base.py`（只同步可选structured replay结算签名）、`_invocation_streaming.py`（只同步共享finalization签名并直接复用既有event helper）、`_settlement_contracts.py`、`_settlement_evidence_models.py`、`_structured_settlement_evidence_models.py`（独占structured started/final evidence投影）、`_settlement_validation.py`、`_settlement_evidence_validation.py`、`_settlement_publication.py`、`_router_contracts.py`、`_router_current.py`、`_router_snapshot.py`、`_router_snapshot_chain.py`（只把冻结的`max_structured_repair_attempts`带入既有chain snapshot）、`route_chain_identity.py`、`models/__init__.py`及`storage/_structured_usage_evidence_repository.py`。三个共享兼容owner不得实现structured chain或structured streaming；交付manifest内全部Python生产文件最终均不超过500有效代码行。
- `adapters/models/fake.py`、`adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_client.py`、`adapters/models/_pydantic_ai_structured.py`（独占SDK structured事件归一化与单次prepared call），不得用模糊“必要时新增helper”代替实际owner。
- `runtime/services.py`、`runtime/_shared_budget_snapshot.py`，以及red contract证明必须修改的现有storage durable validator。模型层 settlement contracts/evidence models/validators 是已知owner，不得降级到本兜底。
- 公共structured seam稳定后修改`templates/service-app/agents/examples/dev_assistant/schemas.py`与`agent.py`：把会显式生成宽松object的`dict[str, object]`迁移为只覆盖现有read/write/shell完成载荷的严格`DevAssistantToolResult`，不改工具授权、执行或引用语义。
- 同阶段修改`templates/service-app/agents/examples/rag_assistant/schemas.py`与`agent.py`：把`assembly_truncation: dict[str, int]`迁移为两个互斥exact object的封闭联合。`RagAssemblyTruncationEmpty`固定为`{}`，仅与`status=no_source`、空citations/source refs、`assembly_id/model_provider=null`组合，并只由未创建Context Assembly的本地无检索结果分支构造；`RagAssemblyTruncation`只含`input_count/retained_count/truncated_count/dropped_count/used_tokens/fragment_count`六个必填非bool非负整数，仅与`status=completed`、非空`assembly_id/model_provider`组合，并只映射既有Context Assembly producer。输出DTO在模型边界校验这些跨字段不变量，拒绝部分六字段、状态与变体混搭及额外字段；不得伪造六个零计数或assembly耐久记录，也不得改动耐久组裁schema或RAG业务语义。

**测试/eval**

- 新增`tests/contracts/test_provider_neutral_structured_policy_contracts.py`，从公开bound seam覆盖DENY零调用、REQUIRE_APPROVAL零claim/零send、durable grant恢复、schema/repair/request/grant/lease篡改拒绝及批准后hard bounds重算；测试不得直接调用私有协调器或伪造成功settlement。
- 新建schema identity、public bound seam、provider doubles、repair/budget、durable replay/recovery、SQLite/PostgreSQL、adapter boundary、negative path与eval contracts；既有text/stream/route-chain回归只作兼容证明。Agent descriptor新增必填identity直接影响的精确owner包括`tests/contracts/{test_agent_registry_router_model_contracts,agent_delegation_service_identity_test_support,auth_policy_hitl_contract_helpers,controlled_multi_provider_failover_test_support,controlled_real_model_policy_approval_test_support,controlled_real_model_retry_budget_test_support,test_controlled_real_model_budget_snapshot_contracts,test_controlled_real_model_fallback_contracts,test_controlled_real_model_runtime_composition_contracts}.py`以及`scripts/{live_model_schema_identity,live_model_stream_execution,smoke_live_model,smoke_live_model_failover}.py`；`live_model_schema_identity.py`只给既有真实文本入口提供Registry identity，不授权structured live调用。`scripts/acceptance_matrix_policy.py`同步固定REQ-028/AC-096～103的证据状态策略。逐个fixture必须显式提供与测试schema匹配的identity，禁止默认/null/伪digest规避必填API。既有真实schema兼容另精确覆盖`tests/contracts/test_agent_registry_schema_contracts.py`、`test_agent_scaffold_validation_atomicity_contracts.py`、`test_example_agent_registry_execution_contracts.py`、`test_dev_approval_flows_contracts.py`、`test_example_agent_flows_contracts.py`、`test_example_eval_migration_contracts.py`、`test_example_agent_policy_provider_contracts.py`、`test_retrieval_doctor_example_contracts.py`与`templates/service-app/tests/test_app_surface.py`，证明Dev Assistant严格工具结果与RAG封闭联合迁移后，全量Registry、公开Agent exact shape、无来源精确`{}`、有来源六个组裁计数、跨字段负路径、离线工具/RAG流程与eval不退化。

**维护文档**

- Product/API/DEV、验收矩阵、living plan/matrix，以及`docs/{adapter-contracts,adapter-contracts.zh-CN,extension-guide,extension-guide.zh-CN,building-an-agent,building-an-agent.zh-CN}.md`。Dev Assistant与RAG Assistant只因严格Registry兼容而在公共seam稳定后迁移，且不得定义核心DTO或改写原业务语义；ticket triage的最终Agent输出含调用后才能生成的trace引用，Phase 19不迁移它，也不为其创建第二套schema控制面。

**不受影响**

- HTTP/OpenAPI/SSE route、CanonicalEvent枚举、tool registry/execution、migration版本、Phase21架构seam、发布/部署配置。

## Testing Seams

1. `AgentRegistry.load_from_directory()`：identity稳定、nested extra封闭、ref/recursive/remote/invalid、全量原子失败。
2. `build_execution_context()`→`BoundModelInvocationService.complete_structured()`：公开red→green主seam，覆盖可信schema、伪造、success、invalid/extra、unsupported、显式route-chain双候选与缩权后单候选拒绝、budget、repair、replay conflict。
3. 两个不同`provider_id`的显式structured doubles：相同DTO协议、不同实现，不通过fake隐式fallback。
4. Pydantic AI adapter窄protocol double：每个`Agent.run(retries=0)`对应一个attempt，禁止SDK output/tool类型越界。
5. Shared-budget direct/allocation与SQLite/PostgreSQL：联合reservation、actual replacement、unknown围栏、exact/conflict replay与durable篡改。
6. Eval：使用Phase 19专用、provider-neutral schema与数据集从公共structured seam评分；invalid/needs-review不得被评为成功。Eval fixture不得定义或导出核心DTO，ticket triage不参与本阶段迁移。
7. 兼容回归：text complete/approved、stream/approved、Phase18.2 chain与`ModelResponse.output_text`。

## Risks / Trade-offs

- [Pydantic schema可能含复杂`$defs/$ref`] → compiler只内联有限本地非递归ref，拒绝递归/remote；用真实四个示例和negative fixtures锁定，不做不完整解释。
- [新增直接`jsonschema`依赖] → 当前lock/compliance已有4.26.0，声明兼容范围并跑lock/license/build；不使用未声明transitive依赖。
- [联合transport×repair reservation偏保守] → 安全优先；调用方可把repair缩到0，actual完整后退款。不得低报最坏成本。
- [Repair prompt缺少invalid原文可能降低成功率] → 只传稳定validation codes和原prompt/schema，避免把可能含secret的生成结果再次发送；质量由deterministic eval验证。
- [拒绝任意显式route-chain降低可用性] → 明确换取不触碰Phase18.2 chain identity/执行顺序，并避免跨provider重复生成或把缩权后单candidate错误降级为legacy；structured route-chain/fallback留给后续独立change。
- [现有大范围settlement validator改动风险] → 新字段保持nullable判别，text路径必须逐字兼容；public seam红灯、聚焦数据库和全量回归共同门禁。
- [Pydantic AI raw JSON输出不等同provider native JSON mode] → 核心正确性不依赖native模式；未来adapter优化必须保持每次请求可计量且不改变公共协议。

## Migration Plan

1. 先以public-seam red contracts冻结DTO、schema identity、capability、reservation、repair与replay失败。
2. 增加直接依赖和Registry catalog；保持旧descriptor ref字段与所有现有Agent config兼容。
3. 接入current/snapshot route与shared-budget联合reservation，再接bound façade和两个provider implementations。
4. 扩展durable response/evidence/replay validators；用当前SQLite/PostgreSQL schema验证，无Alembic migration。
5. 最后迁移eval/示例和维护文档，跑全部门禁并冻结实现`1+2`审查。

回滚时删除structured capability/入口/nullable字段并保留旧text行为；已存在structured durable result的版本不允许旧binary自动执行或解释，运维必须先停止新structured流量并保留证据。当前change不部署，因此本轮rollback只需撤销未提交diff；不得删除耐久数据。

## Open Questions

- 无阻断性问题。若red contract证明Pydantic schema需要递归ref、现有JSON列不足或Pydantic AI raw调用无法逐attempt取得usage，则视为契约变化，必须暂停实现并重新审查，而不是现场放宽。
