## ADDED Requirements

### Requirement: 结构化调用的全部请求进入统一usage evidence
结构化调用 SHALL 复用既有`model.request.started`与唯一`model.usage.updated`生命周期。Schema/route/budget冻结并创建claim后，started/final decision在既有exact route、`attempts[]`与`budget_charge`之外，MUST 携带`structured-output-evidence-v1` exact `structured_output`摘要；text evidence固定为null。摘要exact keys为`schema_version/schema_identity/status/repair_limit/repair_count/provider_request_limit/provider_request_count/replay_identity/validation_issues/error_code`，unknown/缺失字段关闭失败。Schema identity为完整`output-schema-identity-v1`；status只允许`started|valid|invalid|extra_fields|repair_exhausted|failed|needs_review`；repair limit为非bool `0..2`；provider request limit为正整数且等于冻结联合上限。确定终态的repair/request count分别为不超过各自limit的非bool非负整数；只有needs review允许每个count独立为该exact整数或null，null唯一表示该维度无法由耐久事实证明。Replay identity与error code为字符串或null。

`validation_issues`是按`(path,code)`升序、去重的exact object列表，每项只含`code/path`：code只允许`json_invalid|missing_required|type_mismatch|extra_field|value_not_allowed|constraint_violation|schema_invalid|validation_issue_overflow`，path为RFC 6901 instance pointer且根为`""`；不得保存raw value、provider文本或异常message。Validator只遍历`Draft202012Validator.iter_errors(value)`直接返回的错误项，绝不递归`ValidationError.context`，也不创建`FormatChecker`；因此`anyOf|oneOf|not`的直接组合器错误只在其absolute path映射一次constraint violation，`allOf`若直接迭代产生叶子错误则只按该叶子keyword/path映射，不额外追加组合器/context错误。Path从每个直接错误的`absolute_path`逐token投影，string token先把`~`替换为`~0`、`/`替换为`~1`，integer token用无前导零十进制，再以`/`连接。`required`不使用message，而从validator声明的required集合减去instance keys，为每个缺失成员在容器path后追加escaped成员token；`additionalProperties`不解析message，而从instance keys减去`properties`及命中`patternProperties`的keys，为每个extra成员追加token。JSON parse错误映射root `json_invalid`；`required/type/additionalProperties/enum|const`分别映射上述四类；`minimum/exclusiveMinimum/maximum/exclusiveMaximum/multipleOf/minLength/maxLength/pattern/minItems/maxItems/uniqueItems/minProperties/maxProperties/anyOf/oneOf/allOf/not/dependentRequired`映射`constraint_violation`；其他validator keyword含`format`在schema compiler阶段即拒绝；schema identity/definition漂移映射root `schema_invalid`。

Structured调用的`ModelResponse.attempts` SHALL 使用provider-neutral判别式`StructuredModelAttemptEvidence`，保留`ModelAttemptEvidence`全部exact基字段并新增必填`structured_output`；普通text attempts继续只使用原基类，序列化形状逐字不增加nullable字段。`structured_output`是拒绝unknown/缺失字段的`structured-output-attempt-v1` exact object，keys固定为`schema_version/schema_identity/phase/repair_ordinal/transport_ordinal/prompt_digest/repair_trigger_codes/validation_codes/not_started_proof/cleanup_status`：schema identity为完整`output-schema-identity-v1`；phase只允许`initial|repair`；repair ordinal为非bool `0..repair_limit`，initial当且仅当0；transport ordinal为非bool正整数且不超过冻结transport limit；prompt digest为64位小写SHA-256；trigger codes为前述validation code的排序去重数组，initial固定空，repair固定非空；validation codes为null或同一词汇的排序去重数组，null表示未取得可验证输出，空数组表示valid，非空表示该attempt确定invalid。`cleanup_status`只允许`not_applicable|completed|failed|unknown`：prepare未返回且核心持有发送前proof时固定not-applicable；prepared已返回且`aclose()`正常返回/抛出或取消分别为completed/failed；进程或commit窗口无法证明close结果时为unknown。Validation codes非null要求基字段`outcome=completed/side_effect_state=started/completion_observed=true`且usage满足确定结算；其他outcome或side-effect组合固定null。进入repair ordinal `n`要求ordinal `n-1`最后一个attempt具有非空validation codes且cleanup completed，当前ordinal全部attempt的trigger codes逐值等于它；同一repair ordinal所有attempt的schema identity、phase、prompt digest和trigger codes必须逐值相同。Repair ordinal从0无缺口，每个ordinal内transport ordinal从1连续，global attempt从1开始并与按`(repair_ordinal,transport_ordinal)`排序后的序位相等。Valid/invalid/extra/exhausted和send后确定failed都要求最后attempt cleanup completed；send后cleanup failed/unknown只能needs-review。Valid终态最后attempt固定validation codes空数组；invalid/extra/exhausted最后attempt固定非空且排序去重后等于final `validation_issues`的code集合；failed/needs-review只能保留已完成attempt的既有codes，不得凭空补写。

`not_started_proof`只允许null或`structured-output-not-started-proof-v1` exact object，keys固定为`schema_version/kind/usage_call_id/operation_identity_digest/route_digest/schema_identity/prompt_digest/attempt/repair_ordinal/transport_ordinal/digest`。Schema version固定；usage call、operation、route、prompt与digest均为64位小写SHA-256，schema identity为完整`output-schema-identity-v1`，attempt/ordinal为前述非bool整数；kind只允许`client_prepare_not_started|cancelled_before_send`。Digest从移除`digest`后的exact object按`structured-canonical-json-v1`计算。Proof所有identity/ordinal/prompt字段必须与started evidence、route、schema及所属attempt逐值匹配。Proof只能由`models/_invocation_structured_execution.py`在入口编排器窄调用的执行协作者内、尚未调用prepared send的核心控制流事实中构造；业务请求、fake脚本、provider原文、adapter或transport classifier均无权提交proof。任何已调用send或收到HTTP response的attempt均固定`not_started_proof=null`并计入provider request count；structured不读取endpoint classifier或推进下一transport ordinal，既有classifier/retry只服务text路径。Attempt的`side_effect_state=not_started`当且仅当核心发送前proof非null，并要求completion false或null、validation codes为null、input/output/cost和budget charge均为零或既有not-started合法零形状；started/unknown attempt固定proof=null。完整核心发送前proof只足以从provider request count排除该attempt；还必须能确定durable mark事务提交状态，才可按actual-zero结算。`DurableMarkStateUnknown`即使带完整`cancelled_before_send` proof，也 MUST 以`model.provider_side_effect_unknown`/`needs_review`、actual token/cost为null并保留direct/allocation reservation与owner ledger围栏；缺失、unknown、篡改或矛盾proof同样进入needs-review并保留reservation。

Proof digest实现与validator MUST 逐字节复算以下移除`digest`后的`structured-canonical-json-v1` golden preimage；完整proof只是在相同exact object中插入所得digest字段，仍按排序键序列化：

```text
{"attempt":1,"kind":"client_prepare_not_started","operation_identity_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","prompt_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","repair_ordinal":0,"route_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","schema_identity":{"digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","schema_ref":"agents.example.schemas:Output","schema_version":"output-schema-identity-v1","version":"1.0.0"},"schema_version":"structured-output-not-started-proof-v1","transport_ordinal":1,"usage_call_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
sha256=940380a8980e64c418b0836366a34520b41e62688610b3e25a288e89940a5d2b
```

完整映射后若unique issues超过64项或任一路径UTF-8超过1024 bytes，摘要唯一折叠为`[{"code":"validation_issue_overflow","path":""}]`；terminal优先级仍按折叠前事实确定。只要折叠前含任一`extra_field`，首次/无repair终态唯一为`extra_fields`与`model.structured_extra_fields`；否则唯一为`invalid`与`model.structured_invalid`。Repair limit至少为1且所有允许repair均invalid时，无论最后issue类别，终态唯一提升为`repair_exhausted`与`model.structured_repair_exhausted`；limit为0绝不使用exhausted。Repair prompt只使用摘要中排序去重的code字符串，不包含path或invalid原文。

终态组合固定为：started使用count 0、repair count 0、null replay/error和空issues；valid使用非null replay、正request count、空issues和null error；invalid/extra_fields使用`repair_limit=0`、`repair_count=0`、非null replay、正request count、非空issues及分别匹配`model.structured_invalid|model.structured_extra_fields`；repair exhausted要求`repair_limit>=1`、`repair_count=repair_limit`、非空issues和`model.structured_repair_exhausted`；failed使用非null replay、exact repair count、0..provider request limit的exact request count、null value以及`model.provider_failed|model.provider_retry_exhausted|model.invocation_cancelled`中唯一匹配的error，issues可保留失败前已确定的有序validation事实，其中零request的retry exhausted要求当前ordinal恰有transport limit个连续`client_prepare_not_started`发送前proof且最后proof完整，取消只有在send前且cleanup成功时才可确定收口；needs review使用非null replay、`model.provider_side_effect_unknown`，repair/request count每个独立使用可证明的exact非负整数或null，issues可保留unknown前已确定的有序validation事实。`attempts[]`记录所有已形成耐久事实的controller attempts；核心为每个transport ordinal重新prepare并在send前事实确定时自行构造proof，adapter无权提交proof。每次到达send边界只允许candidate或`StructuredProviderCallError`携带一个`attempt=1`的provider-local事实，candidate不得重复携带顶层计量，sole attempt是usage/cost/latency唯一来源，核心再映射为global/repair/transport ordinal；adapter不得把多个provider request聚合进一个controller attempt。Send已调用后直接取消、deadline或未知异常由核心形成同ordinal的unknown全局attempt；所有send后路径均停止transport retry。Provider request count在确定终态必须等于已到达send边界、即没有核心发送前proof的attempt数，needs-review若可证明也使用同一等式，否则为null且attempts只是已知下界。已到达send或收到HTTP response的attempt始终计数且不消费classifier；其启用的usage/cost不完整或send后cleanup失败时整个调用立即needs-review并保留reservation。Repair count非null时等于最大repair ordinal或无attempt时0；count为null只表示可能存在未耐久后继ordinal。所有attempt必须满足前述双ordinal连续矩阵与总联合上限。每个已知实际provider request必须对应独立input/output tokens、cost、latency和outcome；聚合usage/cost MUST 覆盖首次请求与全部repair，不能只计最后一次。零调用preflight rejection不创建该摘要或usage claim，公开失败单独固定count 0与null replay。

Prepared cleanup的交叉验证固定为：每个controller attempt都显式耐久前述`cleanup_status`；每个已返回prepared call恰有一次close结果，未返回prepared才可not-applicable。Send前取消且cleanup completed要求`cancelled_before_send` proof、零usage/charge/request和`model.invocation_cancelled` failed；send前cleanup failed/unknown保留相同proof与零request，但error固定`model.provider_failed`。Send后candidate或call error只有cleanup completed才可进入valid/invalid/extra/exhausted或确定failed；send后取消/deadline/未知异常、cleanup failed/unknown都要求已有/核心unknown attempt、`model.provider_side_effect_unknown` needs-review、无valid value且预算不释放。Publication/recovery若缺cleanup status、not-applicable与prepared/send事实矛盾、出现双close、cleanup与terminal矛盾或把send后cleanup失败降级为failed MUST关闭失败且不重发。

#### Scenario: Repair success聚合全部usage
- **WHEN** 首次invalid、第二次valid且两次usage/cost完整
- **THEN** final evidence SHALL 含两个连续attempt、repair count 1、provider request count 2与两次actual聚合，budget charge逐值一致

#### Scenario: Repair exhaustion仍保存actual
- **WHEN** 允许的每次request均确定invalid且usage/cost完整
- **THEN** failure evidence SHALL 保存全部actual attempts并以`model.structured_repair_exhausted`结束，不把失败请求退款为零

#### Scenario: 未知usage进入needs-review
- **WHEN** 任一started request缺失启用的token/cost维度或provider结果未知
- **THEN** final/attempt evidence SHALL 标记unknown charge与未决attempt，repair/request count按耐久事实逐维度使用exact值或null，settlement保持needs-review且不得发布valid structured result

#### Scenario: Durable started与send边界崩溃不伪造request计数
- **WHEN** durable started已提交，但send前后边界没有形成可证明provider request基数的耐久attempt事实便崩溃
- **THEN** final evidence SHALL 使用needs-review、`provider_request_count=null`和非null replay identity；repair count只在可证明时保存exact值，否则也为null，且不得写0或1冒充unknown、退款或自动重发

#### Scenario: Durable mark提交确认未知不按零请求退款
- **WHEN** 核心仍能以`cancelled_before_send`证明provider request count为0，但durable mark事务的commit ack未知
- **THEN** final evidence SHALL 保存`provider_request_count=0`、not-started proof、`model.provider_side_effect_unknown`与needs-review，actual token/cost保持null；不得改写为cancelled failed或actual-zero settlement

#### Scenario: Claim后的确定失败保留完整attempt和结算
- **WHEN** claim建立后发生确定client/prepare发送前失败、当前ordinal每个transport attempt均在send前以`client_prepare_not_started`证明耗尽、具备完整usage的provider failure或可证明无未决副作用的取消
- **THEN** final evidence SHALL 使用failed与唯一匹配的既有稳定error，保存所有attempt及已知actual，按完整事实释放未使用reservation；同一事实若缺proof或usage则必须改为needs-review且不得退款

#### Scenario: 零请求retry耗尽必须有完整not-started proof链
- **WHEN** structured failed声明`model.provider_retry_exhausted`且provider request count为0
- **THEN** 当前repair ordinal SHALL 恰有`transport_attempt_limit`个global/transport连续attempt，每项都有可复算且匹配route/schema/prompt/ordinal的`client_prepare_not_started`核心发送前proof、零usage/charge；任一已到达send/收到HTTP response、缺失或矛盾都不得写零request，未能完整结算时必须needs-review且不得退款

### Requirement: 结构化durable response与evidence在发布前交叉校验
`ModelResponse.structured_output`、usage decision structured摘要、schema/replay identity、attempt count、provider request count、repair count、canonical `output_text/value`与budget charge SHALL 形成exact交叉不变量。Public replay和后台recovery MUST 共用发布前validator；任一缺失、多余、冲突或非法值都保持`result_persisted|needs_review`并停止final publication、telemetry与`mark_published`，不得重调provider。

#### Scenario: Exact durable structured result可补投
- **WHEN** durable response、evidence和charge逐值一致但final event尚未发布
- **THEN** recovery SHALL 只补投同一event并标记published，provider调用数不增加

#### Scenario: response与evidence漂移被拒绝
- **WHEN** value、schema digest、replay identity、repair/request count或attempt charge任一不一致
- **THEN** replay/recovery SHALL fail closed，不返回成功、不发布final且不调用provider
