## ADDED Requirements

### Requirement: 路由链在任何副作用前完整冻结
系统 SHALL 把跨 deployment/provider fallback 表达为非空、有序、最多 8 项的 `(deployment_id, model_id)` route chain。不可变 chain identity exact shape SHALL 使用 `model-route-chain-v1`，包含 64 位小写十六进制 `chain_id`、`text_completion|text_stream` capability、非 bool 的 1～8 `candidate_count`，以及从 1 连续的 `candidates[]`。每项 exact fields 为 ordinal、非空 deployment/provider/model、route/endpoint-policy/catalog/retry/Bulkhead digest、nullable credential ref、catalog ref/version、非负 token bound 与 nullable 非负有限 cost bound。系统 MUST 拒绝 unknown fields、重复 ordinal、count/list 不一致、bool 冒充数字、负数和 NaN/Inf，并在任何 shared-budget mutation、client、DNS、HTTP 或 provider 副作用前计算 identity；reload 只影响新 root run，恢复只能使用原 durable snapshot 和 chain。

`chain_id` MUST 只从 `model-route-chain-id-v1` canonical preimage计算。该 exact object按顺序语义包含：schema version；capability；candidate count；`agent_model_policy` exact object（请求缩权前原始 Agent descriptor 的 `fallback_routes[0]` 兼容投影 `deployment_id/provider/allowed_models/default_model/fallback_models=[]` 与原始完整有序 `fallback_routes[{deployment_id,model_id}]`）；`request_bounds={prompt_utf8_bytes,max_output_tokens}` 两个非 bool 整数；以及与 public identity同序的 `candidates[]` exact objects。每个 preimage candidate只包含 ordinal、deployment/provider/model、五个 route/policy/catalog/retry/Bulkhead digest、nullable credential ref、catalog ref/version、reserved token bound与 `null|canonical decimal string` reserved cost bound；不得包含 `chain_id` 本身。Request 有序子序列只改变 `candidate_count/candidates[]`，MUST NOT把 `agent_model_policy` 的兼容投影或完整授权列表改写成请求选择；Agent `[A,B]`、request `[B]` 的 preimage仍含 A 投影和 `[A,B]`，只有 candidates为 `[B]`。Canonical bytes MUST 使用 UTF-8、`ensure_ascii=false`、`sort_keys=true`、紧凑 separators `(',', ':')`、`allow_nan=false`；列表保持冻结顺序，null使用 JSON null，整数使用 JSON number，cost Decimal使用无 exponent/无无意义尾零/无负零的字符串。所有 string MUST 是 loader已验证 canonical值；摘要不得包含完整 URL、credential value、header、SDK object、prompt内容或当前余额。当前配置与 snapshot恢复 MUST 复用同一 serializer和 golden vectors，其中必须含上述 B-only逐字节向量及错误改写为 B投影的 digest-mismatch负例；恢复时重算并逐值匹配保存的 chain id。候选删除/插入/重排、capability、Agent policy、两个 request bounds或任一 candidate字段变化均须改变摘要，unknown field、float直接编码、nullable漂移或从 current config补值都关闭失败。

`model-route-canonical-json-v1` SHALL 是本 change 所有 JSON SHA-256 preimage 的唯一 serializer，并逐字复用上一段 UTF-8、Unicode、键排序、紧凑分隔符、NaN、null、整数与 canonical string规则。`model-route-attempt-identity-v1`、两类 `model-route-not-started-proof-v1`、`model-route-chain-approval-request-v1` 和 `model-route-chain-approval-grant-v1` 都是 exact object：所有声明字段即使为 null 也必须出现，unknown/缺失字段、bool冒充整数、float、NaN/Inf、环境正规化或默认补值都关闭失败。Attempt exact keys为`schema_version/chain_id/usage_call_id/operation_identity_digest/candidate_ordinal/global_attempt/route_digest/endpoint_policy_digest/retry_policy_digest`。Proof exact keys为`schema_version/chain_id/candidate_ordinal/global_attempt/reason/attempt_side_effect_state/request_sent/http_response_observed/http_status/response_identity_observed/usage_observed/text_observed/delta_observed/completion_observed/endpoint_policy_digest/classifier_ref/classifier_version`。Approval request exact keys为`schema_version/chain_id/candidate_ordinal/route_digest/usage_call_id/operation_identity_digest/tenant_id/run_id/agent_id/request_id/trace_id/action/resource/arguments_ref/arguments_hash`；`request_id/trace_id`允许 JSON null但不得省略。Approval grant exact keys为`schema_version/request_binding_digest/usage_call_id/operation_identity_digest/approval_id/lease_id/tenant_id/identity_id/agent_id/run_id/action/resource/arguments_hash`，其中`request_binding_digest`必须等于前述 request bytes 的 SHA-256。

以下六行 SHALL 是 UTF-8 exact golden bytes；实现与 validator MUST 逐字节得到列明 SHA-256，不得用 DTO 默认 dump 或另一 serializer 替代：

```text
attempt={"candidate_ordinal":1,"chain_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","endpoint_policy_digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","global_attempt":1,"operation_identity_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","retry_policy_digest":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","route_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","schema_version":"model-route-attempt-identity-v1","usage_call_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"} sha256=d5591241b4786cb8142642e58f7b7e295f46a1ed0c0ea2a8599bfa4a3f0eaa21
client_proof={"attempt_side_effect_state":"not_started","candidate_ordinal":1,"chain_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","classifier_ref":null,"classifier_version":null,"completion_observed":null,"delta_observed":false,"endpoint_policy_digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","global_attempt":1,"http_response_observed":false,"http_status":null,"reason":"client_not_started","request_sent":false,"response_identity_observed":false,"schema_version":"model-route-not-started-proof-v1","text_observed":false,"usage_observed":false} sha256=9acc29f454c47d773bb692ae5046b97b00bfc218f273b10a7399f2b18bd6fb5b
trusted_proof={"attempt_side_effect_state":"started","candidate_ordinal":1,"chain_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","classifier_ref":"trusted_response_header_not_started","classifier_version":"v1","completion_observed":false,"delta_observed":false,"endpoint_policy_digest":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","global_attempt":1,"http_response_observed":true,"http_status":429,"reason":"trusted_business_not_started","request_sent":true,"response_identity_observed":false,"schema_version":"model-route-not-started-proof-v1","text_observed":false,"usage_observed":false} sha256=fe2a4837c90958ca36427e6f7cd7b088bb2361a78515b4bdbedd3ceeb1c0a8c0
approval_request={"action":"model.invoke","agent_id":"agent-a","arguments_hash":"1111111111111111111111111111111111111111111111111111111111111111","arguments_ref":"artifact://arguments-a","candidate_ordinal":1,"chain_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","operation_identity_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","request_id":"request-a","resource":"agent:agent-a:model","route_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","run_id":"run-a","schema_version":"model-route-chain-approval-request-v1","tenant_id":"tenant-a","trace_id":"trace-a","usage_call_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"} sha256=802a004b444cdaf72c5dc0ad4a42bd71fc12f8a1e778b9e57745f2694a66ab82
approval_request_unicode={"action":"model.invoke","agent_id":"agent-a","arguments_hash":"1111111111111111111111111111111111111111111111111111111111111111","arguments_ref":"artifact://arguments-a/参数","candidate_ordinal":1,"chain_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","operation_identity_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","request_id":"request-a","resource":"agent:agent-a:model","route_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","run_id":"run-a","schema_version":"model-route-chain-approval-request-v1","tenant_id":"tenant-a","trace_id":"trace-a","usage_call_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"} sha256=20dfca2bea60ee5a5cf4565a339a13eafcbe2de2f52fc41b95ab91a9445c4297
approval_grant={"action":"model.invoke","agent_id":"agent-a","approval_id":"approval-a","arguments_hash":"1111111111111111111111111111111111111111111111111111111111111111","identity_id":"identity-a","lease_id":"lease-a","operation_identity_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","request_binding_digest":"802a004b444cdaf72c5dc0ad4a42bd71fc12f8a1e778b9e57745f2694a66ab82","resource":"agent:agent-a:model","run_id":"run-a","schema_version":"model-route-chain-approval-grant-v1","tenant_id":"tenant-a","usage_call_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"} sha256=d743ba666b06b5ce289da94503f8d39f85d9eb0da57fc058b7f1c085f9c0b782
```

`approval_request_unicode` 的 `arguments_ref` 是loader已验证的合法canonical string；其`参数`两字必须直接编码为UTF-8。错误的`ensure_ascii=true`会得到SHA-256=`bc72de9dea927cc0e34afad8667f682b16d8f76eb2b105f9c0c0bdaf13579864`，MUST作为digest mismatch拒绝。Current planning、snapshot recovery、direct/allocation、SQLite/PostgreSQL repository、approval handoff与公开 evidence validator MUST 共用全部六组vectors，并覆盖UTF-8/Unicode原文与转义差异、键序输入等价、unknown/缺失/null省略、bool ordinal/status、非有限值、任一identity/proof/binding字段篡改和request/grant交叉绑定不一致的负合同。

#### Scenario: 两个不同 deployment/provider 候选形成稳定顺序
- **WHEN** typed settings 与 Agent policy 声明两个不同 deployment/provider 的合法 route refs
- **THEN** Router 返回 ordinal 为 1、2 的 frozen chain，且每项逐值绑定各自 deployment 的 endpoint、credential、catalog、价格、retry 和 Bulkhead identity
- **AND** 同一输入和冻结 snapshot 产生相同 chain identity

#### Scenario: reload 不改变旧 run
- **WHEN** 一个 run 已冻结 chain，随后当前配置新增、删除、替换凭据或重排 route
- **THEN** 旧 run 的恢复仍只读取原 chain/ordinal/catalog identity
- **AND** 新配置不能被用来补齐或替换旧候选

### Requirement: deployment、Agent 与请求逐层只缩权
Agent policy SHALL 以有序 `fallback_routes` 声明最大候选集；请求省略 route refs 时使用该冻结顺序，提供 route refs 时 MUST 是其非空有序子序列。请求 MUST NOT 添加或重排 deployment/provider/model，不得声明 endpoint、credential、catalog、Bulkhead 或 SDK adapter。旧同 deployment `default_model/fallback_models` MAY 精确投影为同一 deployment 的 route refs，但不得由此推断其他 deployment。

#### Scenario: 请求选择有序子序列
- **WHEN** Agent chain 为 A、B、C，request 只选择 A、C
- **THEN** frozen chain 为 A、C 并保留相对顺序
- **AND** B 被删除不会授权任何新 route

#### Scenario: 请求删除原始首 route 不改写 Agent 投影
- **WHEN** Agent chain 为 A、B，兼容投影逐值绑定 A，而 request 只选择 B
- **THEN** frozen chain 的唯一 candidate 是 B，但 chain-id preimage 的 `agent_model_policy` 仍逐值保存 A 兼容投影与原始 `[A,B]`
- **AND** current planning与 snapshot recovery使用同一 golden bytes；把投影改写为 B、把 fallback routes缩成 `[B]`或降级为 legacy均关闭失败

#### Scenario: 请求插入或重排候选
- **WHEN** request 提供 A、X 或 C、A，或者通过旧 provider/deployment 字段与 route refs 表达冲突选择
- **THEN** Router 在 reservation/client/network 前以 `model.route_not_allowed` 或 `model.request_invalid` 拒绝
- **AND** provider、permit、client 与预算副作用均为零

### Requirement: 每个候选的资源与安全身份隔离
每个候选 MUST 只使用自身 deployment 派生的 credential forwarding policy、endpoint、client lease、catalog、price、retry、deadline 与 `process_deployment` Bulkhead。不同 deployment 即使 provider kind 或 model 相同，也 MUST NOT 共享 credential-bearing client lease、permit 或 catalog identity。SDK object、secret、完整 endpoint、header 与 raw response MUST 留在 adapter 内。

#### Scenario: 两个 deployment 使用不同安全边界
- **WHEN** 两个候选分别配置不同 endpoint、credential、catalog 与 Bulkhead
- **THEN** composition/adapter double 观察到两个独立 lazy client lease、transport credential binding 与 semaphore scope
- **AND** 任一候选都无法读取或转发另一候选的 secret、origin 或价格

#### Scenario: 关闭和 reload 不串扰候选
- **WHEN** 一个候选正在 prepare/stream，另一个候选从未构造 client，随后 composition close 或 reload
- **THEN** close 等待并清理已拥有资源且不构造未使用候选 client
- **AND** reload 不替换 active run 的 credential、client 或 permit identity

### Requirement: 运行时只接受两类可信 not-started 切换
显式route chain的调用顺序 MUST唯一为`hard eligibility → candidate policy/audit → candidate reservation → durable attempt_lifecycle started identity → Bulkhead permit → candidate-isolated client lease → send/iterate`；legacy单route保持既有permit/client-before-mark顺序。下文所有chain `client_not_started`都表示started identity已提交后，permit/client/prepare在send前确定失败并以同一UoW关闭对应lifecycle，不允许删除started record。

静态 hard eligibility 失败 MAY 在零 provider 副作用时跳过候选。每个首次调用或同 route retry 在取得 client、进入 send 或其他 provider side-effect boundary 前，MUST 先在 owner UoW/CAS 中为本次全局 attempt 追加不可覆盖的耐久 `attempt_lifecycle` started record；该 record 以 `model-route-attempt-identity-v1` exact object经`model-route-canonical-json-v1`计算的digest绑定 chain、usage/operation identity、candidate ordinal、global attempt、route/endpoint/retry digest，初始 `request_sent/http_response_observed/response_identity/usage/text/delta=false`且 `side_effect_state=not_started`。相同 identity 的 commit-ack replay只返回原 record；identity冲突关闭失败。started mark已提交后，无论 request_sent仍为false、已经发送，还是提交确认未知，恢复都不得把该 attempt 当作尚未开始而自动重发；它必须由同一 UoW原子关闭为`not_started_proven|unknown|settled`，否则保留reservation并进入needs-review。

每个实际 attempt 只允许两类封闭推进事实：一是 client/send 前可证明请求尚未越过进程边界的 `client_not_started`，对应该 attempt 的 `side_effect_state=not_started`、`request_sent=false`、`http_response_observed=false`、`completion_observed=null`；二是请求已发送并收到 HTTP response 后，由当前 endpoint policy/version 绑定的 `trusted_response_header_not_started/v1`、当前 deployment 显式非空且包含该状态码的 `cross_provider_failover_http_statuses`，以及“无 response identity/usage/text/delta”共同证明业务执行与计费均未开始的 `trusted_business_not_started`，对应该 attempt、当前 candidate 与调用级 claim 高水位的 `side_effect_state=started`、`request_sent=true`、`http_response_observed=true`、`completion_observed=false`。两类 proof 都 MUST 生成去敏 `not_started_proof_digest`，并在下一次同 route retry、transfer 或 terminal 前，以全局 attempt 为键把 candidate `not_started_proofs[]`、durable attempt/settlement state和同一 lifecycle的`started→not_started_proven`原子提交；claim 高水位、逐候选聚合高水位与逐 attempt 事实分别保持单调，第二类不得回写成未发送或把 claim/candidate 从 started 降级。前序候选或同候选早期 retry 已使高水位 started 时，后续 attempt 仍可凭自身 `client_not_started` proof 安全收敛，但不能抹除早期 started/response；不得用聚合高水位替代单次 attempt 资格，也不得重放已存在 started identity 的 attempt。只有当前候选从首次到末次的每个 lifecycle record都连续且全部为`not_started_proven`、与两类 proof逐值一一匹配且没有`started|unknown|settled`冲突项，才可 actual-zero 收敛并跨 provider。未配置 classifier/状态白名单、仅凭 403/429/5xx 状态或 body/异常文本、write/read timeout、取消、response identity、usage、text、delta及任何无法证明的 transport 结果 MUST 停止 chain。取消只有在provider-neutral stream关闭结果证明`stopped`、usage `finality=complete`且启用维度完整，并且不存在durable delta intent或发布确认不明时，才可按actual usage收敛为`cancelled/invocation_cancelled`；该终态不授权retry/fallback。其他关闭结果保持原稳定错误或进入unknown。Harness `PolicyEngine` 的 deny 不是 provider HTTP response，MUST 直接终止且不得借 classifier 绕过。

#### Scenario: 显式取消只以可信完整关闭结果结算
- **WHEN** route-chain stream因显式取消或冻结deadline结束，关闭结果证明远端`stopped`、usage `finality=complete`且所有启用维度完整，并且durable stream group中没有delta intent、`result_persisted`前缀或发布确认不明
- **THEN** owner UoW以actual usage结算当前attempt，lifecycle为`settled/result_committed`、candidate为`cancelled/invocation_cancelled/result_committed`，selected/active/waiting为空且current reservation为canonical空
- **AND** 不追加新transition、不发布`model.output.completed`、不调用当前或后继provider；usage不完整、关闭unknown/失败或存在durable delta不确定性时仍保留reservation/capacity并进入needs-review

每条 `not_started_proof_digest` MUST 为 `model-route-not-started-proof-v1` exact object经`model-route-canonical-json-v1`所得 bytes 的 SHA-256；输入 exact fields 为 schema version、chain id、candidate ordinal、全局 attempt、reason、attempt side-effect state、request-sent、HTTP-response-observed、nullable HTTP status、response-identity/usage/text/delta observed 四个 bool、nullable completion-observed、endpoint-policy digest及 nullable classifier ref/version。`client_not_started` 的 attempt side-effect 必须为 not_started且 status/classifier 为 null；`trusted_business_not_started` 的 attempt side-effect 必须为 started、status 必须命中冻结白名单且 classifier 必须逐值匹配当前 endpoint policy。raw header/body/error 不进入 state 或 digest input；相同 candidate 的 proof records 按全局 attempt 严格递增，缺失、覆盖、重复、重排或与 durable `attempts[]` 任一字段不一致都关闭失败。

#### Scenario: client 前失败后只调用下一候选一次
- **WHEN** 首候选在 client/send 前确定失败，次候选可执行
- **THEN** 首候选以去敏 not-started reason 收敛，次候选恰好调用一次并可完成
- **AND** 不调用第三候选或 fake

#### Scenario: 受信 429 或 503 证明业务未开始
- **WHEN** 当前候选已发送请求并收到 429 或 503，状态码被当前 deployment 的跨 provider 白名单显式允许，端点绑定 classifier 返回唯一合法 header，且没有 response identity、usage、text 或 delta
- **THEN** runtime 保留 `request_sent=true/http_response_observed=true/side_effect_state=started` 历史，以 `trusted_business_not_started` 和 proof digest 原子转移到下一候选
- **AND** 同候选 retry 仍先按冻结 retry policy 消耗；attempt 耗尽或该状态不再同 route retry 时才推进 ordinal

#### Scenario: 同候选多次受信 retry 后再跨 provider
- **WHEN** 当前候选的 attempt 1 与 attempt 2 都收到具备完整 trusted-business proof 的白名单 response，冻结 retry policy 要求 attempt 1 先同 route retry并在 attempt 2 后耗尽
- **THEN** runtime 在发起 attempt 2 前先耐久追加 attempt 1 proof，并在 transfer 前追加 attempt 2 proof；两条 record 的全局 attempt、digest 和观察事实均不可覆盖且逐值映射 durable `attempts[]`
- **AND** candidate/claim 高水位保持 started，两个 attempts 均以 actual zero 结算后才原子进入下一 ordinal；任一 proof 缺失、覆盖、重排或 commit-ack unknown 都不退款、不重发、不调用后继 provider

#### Scenario: 第二次 retry 的 started mark 后崩溃
- **WHEN** attempt 1 已以 matching proof关闭为`not_started_proven`，attempt 2 的耐久 started identity已提交，而进程在 send前或 send后、proof/settlement提交前崩溃
- **THEN** SQLite 与 PostgreSQL恢复均把 attempt 2 视为已开始并保留当前reservation，进入needs-review或只补投同一可信settlement
- **AND** 不重复发送attempt 2、不创建attempt 3，也不调用任何后继provider；started mark或关闭写入的commit-ack丢失只按同一identity重放读取

#### Scenario: 403 必须显式选择
- **WHEN** provider 返回带合法 not-started header 的 403，但当前 deployment 没有把 403 写入 `cross_provider_failover_http_statuses`
- **THEN** runtime 不跨 provider 切换；只有显式列入 403 且其他受信条件全部成立时才可按 `trusted_business_not_started` 推进
- **AND** Harness 自身 policy deny 始终终止，不能映射成该 provider 场景

#### Scenario: 模糊 timeout 或不受信 response 禁止后续 provider
- **WHEN** 当前候选在可能发送后发生 write/read timeout、取消、缺少合法 classifier 的 HTTP response，或出现 response identity、usage、text、delta及其他无法证明的 transport 结果
- **THEN** runtime 停止 chain，保留当前 reservation/needs-review 或原稳定错误
- **AND** 后续所有 provider 调用次数为零

#### Scenario: 三候选先受信业务未开始再 client 前失败
- **WHEN** 候选 A 以 `trusted_business_not_started` actual zero 收敛，候选 B 随后以 `client_not_started` 收敛，候选 C 可执行
- **THEN** claim 高水位保持 started，A/B 的逐候选 side-effect/request/response/proof 历史分别保持 started/已响应与 not-started/未发送，C 恰好调用一次
- **AND** 不回退 claim、不重放 A/B，也不因调用级高水位阻止 B 的安全 transfer

#### Scenario: 三候选先 client 前失败再受信业务未开始
- **WHEN** 候选 A 以 `client_not_started` 收敛，候选 B 随后以 `trusted_business_not_started` actual zero 收敛，候选 C 可执行
- **THEN** A/B 的逐候选事实与两份 proof 独立耐久，claim 高水位在 B started 后单调提升并保持 started，C 恰好调用一次
- **AND** 全局 attempt、transition 与 reservation sequence 连续且不创建第二笔 claim

### Requirement: reservation transfer 与候选推进原子且可重放
系统 SHALL 使用同一 usage stable key 和 shared-budget claim 持有整条 chain。进入下一候选前，当前候选从首次到末次的每个耐久 lifecycle record MUST 连续、全部以 `not_started_proven`关闭，并各自与 `client_not_started|trusted_business_not_started` proof record逐值匹配；proof list、attempt lifecycle关闭、当前候选聚合状态、按冻结顺序评估的后继状态、下一 active ordinal或 exhausted终态、claim reservation、owner ledger impact 与 `model-route-chain-state-v1` MUST 在一个 UoW 原子替换。`soft_budget` 只表示既有 soft review threshold 对该 frozen candidate选择有限 fallback，不等于 deny/require-approval；`balance` 只表示在 owner lock/CAS中用该候选冻结 bound替换当前 impact时余额不足。两者都必须耐久编码为 `budget_ineligible`，零 actual attempt/proof/reservation/permit/client/provider，并继续检查更后候选。末次 `client_not_started` 只证明该 attempt 未发送；调用级或 candidate 高水位 MAY 因前序 candidate/attempt 的 `trusted_business_not_started` 保持 started且不得回退。每条 trusted-business proof 都必须保留对应 request/response 历史并由端点绑定 classifier、状态白名单和零生成/计量事实证明可按 actual zero 结算。除此之外的 started、HTTP response、unknown、`settled`非零实际或悬空 lifecycle MUST 保留当前 reservation并把 claim/ledger 提升为 needs-review或按可信 actual结算；不得退款、记零、创建第二笔 claim或推进 ordinal。相同 attempt-start/close、transfer或terminal可幂等重放，任何 chain/ordinal/attempt identity/bound/reason/proof-list/lifecycle不一致 MUST fail closed；exact replay必须先返回首次 durable budget decision，并在任何后继调用前从冻结chain的canonical bytes重算全部历史attempt identity与not-started proof digest。仅让被篡改的digest引用在state内彼此一致、数量和字段形状合法，不构成可信恢复状态；与冻结chain重算值不一致时必须冲突并保持后继零调用，不得按恢复时余额重选候选。

#### Scenario: 下一候选价格和上界不同
- **WHEN** 首候选 reservation 为 40 tokens/1 cost，次候选为 70 tokens/3 cost 且余额足够
- **THEN** transfer 后 ledger impact 只反映次候选 70/3，chain state 将首候选标为 not_started、次候选标为 active
- **AND** 不存在同时占用 40+70 或先释放为零的可观察提交

#### Scenario: 中间候选预算不可用时原子直达后继
- **WHEN** 当前 A 已可信 not-started，B 的 soft threshold选择 fallback或以 B 上界替换 impact时余额不足，而更后的 C 可预约
- **THEN** 同一 owner lock/CAS UoW 把 A 置 not_started、B 置 `budget_ineligible/reason=soft_budget|balance`，并只以一条 A→C transition原子替换为 C reservation
- **AND** B 的 attempt/proof/reservation/permit/client/provider调用均为零，不存在 A先退款或 A/B/C同时占额的可观察提交

#### Scenario: 后继全部预算不可用时原子耗尽
- **WHEN** 当前 A 已可信 not-started，所有有序后继均为 static或budget ineligible且没有可预约候选
- **THEN** 同一 UoW actual-zero结算 A、释放原 reservation、耐久保存全部 skip state、清空 current reservation，并追加唯一 A→null `route_exhausted` terminal transition
- **AND** exact replay在 SQLite/PostgreSQL均返回同一 causes、ordinal、ledger impact和 terminal，不按后来余额重选或调用 provider

#### Scenario: 相同 operation 崩溃后重放
- **WHEN** 进程在 transfer 提交确认丢失后以相同 stable key、chain identity 与 ordinal 恢复
- **THEN** SQLite 与 PostgreSQL 都返回同一 durable active candidate 和 reservation
- **AND** 不重复扣款、不新建 claim、不重放已开始 provider

#### Scenario: 形状合法的历史摘要篡改仍关闭失败
- **WHEN** 恢复输入保持attempt/proof数量、ordinal和相互引用一致，但把任一历史attempt identity digest，或proof digest及其lifecycle引用同步替换为非冻结chain重算值
- **THEN** completion与streaming恢复都在调用后继provider前返回稳定冲突并保留现有reservation
- **AND** 后继provider调用次数为零，不把引用自洽当作canonical identity有效

#### Scenario: unknown 不退款不重放
- **WHEN** active candidate 已 started 后结果未知
- **THEN** claim、ledger、usage outbox 与 provider lease 保持 needs-review/未决 impact
- **AND** exact replay 返回 needs-review，后续 provider 调用次数为零

#### Scenario: 无可信业务未开始证明时拒绝 transfer
- **WHEN** SQLite 或 PostgreSQL 中 active candidate 自身已 started且没有完整 `trusted_business_not_started` proof，或已经出现 response identity、usage、text、delta、unknown
- **THEN** transfer 在 SQLite 与 PostgreSQL 均关闭失败并保留当前 reservation
- **AND** 后继 provider 调用次数为零

### Requirement: 每个候选独立执行 Policy/HITL
每个 candidate SHALL 在获得有预算影响的 reservation、permit 或 client 前独立执行 `PolicyEngine`；前一候选的 allow 或 approval MUST NOT 授权后继。固定决策顺序为 static eligibility后执行候选级 policy：deny终止，require-approval暂停，soft review threshold选择有限 fallback时记 `budget_ineligible/soft_budget`并继续，allow才进入 owner lock下的 current-balance reservation，余额不足记 `budget_ineligible/balance`并继续。Route-chain invocation MUST 在首次可信 bound entry、任何 policy/coordination row/approval record 前，从受信 tenant/run/request/agent/trace 和原始语义 `operation_key` 生成并冻结唯一 64 位小写 SHA-256 `usage_call_id` 与 `operation_identity_digest`。首候选直接 require approval 时，shared-budget UoW MUST 以该 ID 建立 token/cost impact 均为 0、`side_effect_state=not_started` 的 coordination claim/allocation并保存完整 chain state；该 row 不构成 model reservation。后继返回 `require_approval` 时，同一 shared-budget UoW MUST 释放前一 reservation、把 impact 置零并保存 waiting ordinal 与 `approval_request_binding_digest`，但不得建立目标 reservation或调用 provider。request binding 使用 `model-route-chain-approval-request-v1`，exact fields 为 chain id、candidate ordinal、route digest、usage call id、operation identity digest、tenant/run/agent/request/trace、action、resource、arguments ref/hash；此时 approval id 与 lease id 尚不存在，`approval_grant_binding_digest` 必须为空。原始 operation key与请求只由既有 checkpoint/私有 arguments artifact/ref 保存，不得进入 public chain state；完整候选只从冻结 `budget-tree-v2` snapshot恢复，chain id、usage call id与 operation identity digest 必须逐值复算。

Orchestrator/ApprovalService SHALL 按既有独立 UoW 顺序创建带同一 request digest、usage call id 与 operation identity digest 的 approval record并独占提交 resolution lease。`complete_approved()`/`stream_approved()` MUST 从受信 checkpoint 取回原始 operation key和上下文，使用既有 `stable_usage_call_id()` 重算初始 ID，并逐值校验 waiting state、active lease、approval metadata 与 request digest，再生成 `model-route-chain-approval-grant-v1` digest；其 exact fields 为 request digest、usage call id、operation identity digest、approval id、lease id、tenant/identity/agent/run/action/resource 与 arguments hash。随后 shared-budget UoW 以 waiting ordinal/request digest/usage call id CAS并重检该 ordinal current balance：足够时原子保存 grant digest、替换目标 reservation并激活 ordinal；不足时保存 grant digest与 `budget_ineligible/balance`、保持零 impact，并只允许 candidate controller对更后 ordinal重新执行独立 policy，grant不得复用。该 UoW MUST NOT 声称消费或完成另一个 UoW 所属的 approval lease。lease fencing/finalization 仍由 ApprovalService/repository 拥有，两个 UoW 通过同一 frozen usage identity、grant digest 与幂等 recovery 衔接。Route-chain approval MUST 复用最初的 claim/settlement/outbox/stream group，MUST NOT 改用 legacy `operation_key="approved:<approval_id>"` 生成新 ID，不得 rekey、建立映射或创建第二 claim。grant 只能缩权，不能改 route、顺序、参数、identity 或上界；usage identity不匹配、不同 approval/lease/grant 在 activation 前后均 MUST fail closed且零新增 reservation/provider。

#### Scenario: 获批候选余额不足不复用授权
- **WHEN** waiting candidate B 获得合法 grant，但 activation UoW重检 current owner balance后 B 不可预约且后面还有 C
- **THEN** B 以 `budget_ineligible/balance`、零 reservation/provider耐久收敛，C 必须重新执行自己的 Policy/HITL
- **AND** B 的 grant不授权 C；commit-ack或 recovery只重放 B 的首次 balance决定，不按新余额激活 B

`usage_call_id` SHALL 精确复用既有 `stable_usage_call_id(context, operation_key)` 的 `usage-v1` canonical。`operation_identity_digest` SHALL 对 `model-route-chain-operation-v1`、tenant id、run id、agent id、request id或空串、trace id、原始 operation key按此顺序以 `U+001F`连接后取 SHA-256；两者都必须是 64位小写十六进制且禁止随机 fallback。

#### Scenario: allow 后下一候选需要审批
- **WHEN** 首候选 allow 后以 `client_not_started|trusted_business_not_started` 安全收敛，第二候选 policy 返回 require approval
- **THEN** runtime 以 `model.approval_required` 暂停，current reservation/ledger impact 为零，第二与后继 provider 调用均为零
- **AND** request binding 逐值绑定 chain、ordinal、route 与原始请求，approval/grant digest 此时为空

#### Scenario: 首候选直接要求审批
- **WHEN** 冻结 chain 后第一候选 policy 返回 require approval
- **THEN** runtime 以首次入口已冻结的 usage call id写入零 impact coordination row、完整 v1 state 和 request binding，provider/permit/client、model reservation 与 grant binding 均为零
- **AND** reload 后只用私有 checkpoint、原 approval artifact 与冻结 root snapshot重算并匹配同一 usage/operation identity和 chain，不读取当前配置

#### Scenario: 审批后恰好继续一次
- **WHEN** ApprovalService 已独占提交 matching lease，shared-budget activation 随后提交 reservation/grant digest但确认丢失
- **THEN** activation只追加一条`from_ordinal=waiting ordinal/to_ordinal=同ordinal/state=approved/reason=approval_granted/released_token_bound=0/released_cost_bound=null/reserved_*=目标冻结bound`的连续transition，并在同一UoW把candidate从waiting直接变为active
- **AND** exact replay 用相同首次 usage call id、operation identity、lease/grant 返回同一 active ordinal、reservation、transition sequence与数组，不重复预算影响、不补写`activated` transition；不同 ID、lease/grant或任一transition字段组合关闭失败
- **AND** 不产生 `approved:<approval_id>` 派生的新 claim，目标 provider 在 durable approved commit 后最多调用一次

#### Scenario: approval 与预算两阶段 handoff 的 crash windows
- **WHEN** 进程分别崩溃在 waiting request binding 后、approval record 创建后、lease claim 提交后或 activation 提交后
- **THEN** 前三者恢复时保持零 provider且只重放同一 request/grant，最后一种只重放相同 activation/settlement
- **AND** 不要求跨 approval/shared-budget UoW 原子提交，不公开 lease，也不允许 takeover lease 覆盖已保存的 grant digest

#### Scenario: 获批后余额不足不伪造 activation transition
- **WHEN** matching grant已保存，但目标candidate在activation UoW中因current balance不足变为`budget_ineligible/balance`
- **THEN** request/grant binding与既有`waiting_approval/approval_required` transition保留，但不得追加`approved|activated` transition或目标reservation
- **AND** 该ordinal成为跨普通skip保持不变的零impact source anchor；后继allow只追加零released的`transferred/balance`，require-approval只追加零released/reserved的`waiting_approval/approval_required`，deny只追加零released/reserved的`terminated/policy_denied`，无可继续候选只追加零released/reserved的`terminated/route_exhausted`
- **AND** commit-ack replay逐值返回首次source/from/to/reason/bound/sequence数组，后继grant/policy不得改写原anchor或复用前一grant

#### Scenario: 初始前导 skip 后重新授权
- **WHEN** 初始扫描跨过任意前导`static_ineligible|budget_ineligible`后，首个执行policy的非首ordinal返回allow、require-approval或deny
- **THEN** 普通skip不成为transition source；allow唯一写null→target的`activated/initial`，require-approval唯一写null→target的零bound`waiting_approval/approval_required`，deny不追加transition并以denied candidate收口
- **AND** SQLite/PostgreSQL、direct/allocation与recovery拒绝把前导skip伪造成current reservation、balance transfer或policy-denied source

#### Scenario: deny、stale、mismatch 或重复 grant
- **WHEN** 任一候选 policy deny，或 grant 已过期、绑定不匹配、已被不同 continuation 消费
- **THEN** deny 使用 `model.policy_denied` 终止且不得进入 route-chain exhausted causes；无效 grant 维持既有 `ValueError("model approval grant ...")` seam
- **AND** 首候选 deny 不建立 coordination/reservation；后继 deny 在同一 UoW 以已知零 charge 结算前序 not-started attempt、释放 reservation并写 rejected failure evidence，不调用该候选或后继 provider

### Requirement: 结果和证据绑定同一耐久 chain
provider-neutral `ModelUsageEvidence.decision` SHALL 保留既有 exact `route`、`attempts[]`、`budget_charge` 并只在 chain mode 增加 exact `route_chain={schema_version:"model-route-chain-evidence-v1",identity:<完整 model-route-chain-v1>,state:<完整 model-route-chain-state-v1>}`；started 与 final/failure 都必须携带它，identity 逐值相同。state exact fields SHALL 包含 chain id/count、64 位小写 SHA-256 usage call id、operation identity digest、互斥的 nullable active/waiting-approval/selected ordinal、始终非空的 evidence route ordinal、delta fence、current reservation、逐候选 identity/state/聚合 `side_effect_state`/reason/request-sent/http-response-observed/http-status/response-identity-observed/usage-observed/text-observed/delta-observed/completion-observed、有序 `not_started_proofs[]`、request-binding/grant-binding digest，以及从 1 连续的 reservation transitions；调用级 claim started 只表示整链高水位，不覆盖当前 candidate 或 attempt 的独立事实。usage call id可公开关联但原始 operation key不得进入 state；两份 approval digest只公开哈希，不公开 approval/lease identity。`decision.route` 和顶层 provider/model MUST 逐值命中每份 state 的 evidence route ordinal。completed 时该 ordinal 等于 selected；cancelled时等于唯一cancelled candidate且selected为空；denied 时等于唯一 denied candidate；exhausted 时等于最后 cause；unknown 时等于最后 active/unknown candidate；尚未激活时等于当前评估 candidate。拒绝 unknown fields、不可能的 identity/state/reason/ordinal/reservation 组合、负数、bool 数字和 NaN/Inf。route-chain `attempts[]` 完整继承 5.29 exact fields，并为每个实际 attempt 强制增加 candidate ordinal/deployment/provider/model、request/HTTP-response/status、response-identity/usage/text/delta observations、completion、nullable not-started reason/proof digest、endpoint-policy digest 与 nullable classifier ref/version；只有 route-chain schema 存在时才接受这些字段，`attempt` 跨候选与同候选 retry 全局从 1 连续、不重置。每个零 charge attempt 必须与对应 candidate proof record 逐值相等并可重算 digest；非 not-started attempt 的 reason/digest 为 null。可信actual取消attempt唯一使用`outcome=cancelled/side_effect_state=started/completion_observed=false`，按完整close usage记录actual charge并以`model.invocation_cancelled`收口；对应lifecycle/candidate/claim为result_committed，不得有response、selected或completed事件。`budget_charge` 继续按全局 attempt 聚合，只有逐 attempt 完整证明的 `client_not_started|trusted_business_not_started` charge 为 0。公开 `model.request.started` 是 provider 副作用前的调用生命周期 evidence，本身不围栏 route；started 与 final 顶层 route 不同时，只能由完整 durable chain 逐值证明所有前驱的全部实际 attempts 安全收敛。证据 MUST NOT 包含 secret、完整 endpoint、header、response id、raw error/body、原始 operation key 或 prompt/output 文本。

Candidate state enum MUST 精确为 `pending|static_ineligible|budget_ineligible|waiting_approval|active|not_started|completed|cancelled|unknown|denied`。静态资格失败唯一使用 `state=static_ineligible/reason=static_ineligible`；候选级 soft-threshold fallback或 owner-balance reservation失败唯一使用 `state=budget_ineligible/reason=soft_budget|balance`。两类 ineligible state 都要求 `side_effect_state=not_started`、观察位全 false、nullable response/completion为空、proof为空，且零实际 attempt、零 reservation；不得把它们编码成 pending、denied或not_started。Static、soft-budget与普通 allow后 balance skip的 approval bindings为空且不单独追加 transition；只有获批候选 activation时 balance不足的 budget-ineligible允许且强制 request/grant两个 binding digest同时非空，禁止单边 binding或把 grant带到后继。该获批特殊形状可保留此前 waiting transition，但不得出现为本 ordinal建立 reservation的 approved/activated transition；继续后继只允许 `from=<本 ordinal>/state=transferred/reason=balance/released=0`，无后继只允许本 ordinal→null route-exhausted terminal。`not_started`只允许至少一个实际 attempt且完整 proof list覆盖全部 attempts。`cancelled`只允许`reason=invocation_cancelled/side_effect_state=result_committed/completion_observed=false`，唯一matching lifecycle为`settled/result_committed`，selected/active/waiting为空、current reservation为canonical空、evidence route ordinal命中该candidate且不追加新transition。初始扫描以同 stable usage identity的零 impact carrier耐久保存预算决定；全资格/预算耗尽的 transitions为空。已有 current的安全耗尽只允许一条 current→null terminal transition，canonical current reservation为空。Evidence route ordinal命中最后 cause。公开 evidence不新增顶层 `ModelUsageEvidence.usage_call_id`；唯一新增位置是完整 state内的 `decision.route_chain.state.usage_call_id`，并须与 CanonicalEvent/telemetry metadata逐值相同。

#### Scenario: 次候选成功证据完整
- **WHEN** 首候选在 client/send 前以 `client_not_started` 收敛，次候选完成并返回可信 usage
- **THEN** final evidence 的 selected ordinal 为 2，首候选 charge 为 0，次候选记录实际 usage/cost/latency 与最终 route
- **AND** reservation transition 与 shared-budget durable state 逐值一致

#### Scenario: evidence 与 durable chain 冲突
- **WHEN** final provider/model、selected ordinal、route digest、transition 或 attempt 与 durable chain 任一不一致
- **THEN** settlement/publication/recovery 以 replay/contract error 关闭失败
- **AND** 不发布 final usage、completed 或 run terminal

### Requirement: 全链耗尽和 fake 边界保持封闭
所有候选均静态不合格、预算不可用或以 `client_not_started|trusted_business_not_started` 安全耗尽时，系统 SHALL 返回 `model.route_chain_exhausted`。error detail exact fields 为 `schema_version="model-route-chain-exhausted-v1"`、64 位小写十六进制 chain id 与按 ordinal 连续的 `causes[]`；每项只有 ordinal 和 `capability|catalog|input_bound|hard_budget|soft_budget|balance|not_started_failure` cause，拒绝 unknown fields、重复 ordinal 和缺口。`static_ineligible`映射前四类，`budget_ineligible`逐值映射 `soft_budget|balance`，有实际可信 not-started attempts才映射 `not_started_failure`。Chain不安全恢复的公开调用错误摘要中，`attempt_count` MUST等于已耐久的全局attempt identity数量；`provider_called` MUST仅在任一attempt已有request、HTTP response、result、usage、text或delta观察事实时为true，因此send前已耐久started identity允许`provider_called=false`且`attempt_count>0`。该chain恢复例外不得放宽既有持久化provider失败证据的四字段一致性校验。Policy deny/require-approval单独返回其稳定结果，绝不进入 exhausted summary。真实 route chain MUST NOT 隐式追加 fake、切换到 fake 文本或把真实失败改写为成功。fake 只能由 profile/Agent/request 交集中的显式 route ref 选择。

#### Scenario: send前悬空started identity不误报provider调用
- **WHEN** 前序候选已安全proof-close并耐久转移，当前active候选的started identity已提交但request、HTTP response、result和delta均未观察，恢复判定该identity不可重发
- **THEN** completion与streaming都返回`provider_called=false`且`attempt_count`等于全链已耐久identity总数
- **AND** 当前attempt不重发，不调用后继provider，也不把正数attempt count改写为provider已调用

#### Scenario: 两个真实候选均安全耗尽
- **WHEN** 两个真实候选分别静态不合格、预算不可用或以 `client_not_started|trusted_business_not_started` 收敛，且 chain 中没有 fake ref
- **THEN** runtime 返回 `model.route_chain_exhausted` 并记录封闭 cause summary
- **AND** FakeModelProvider 调用次数为零

#### Scenario: 显式 fake local route
- **WHEN** local profile 与 Agent policy 只声明显式 fake route
- **THEN** 既有 fake completion/stream/eval/smoke-local 行为保持可用
- **AND** 网络哨兵证明 provider ambient env 与真实 client 均未被读取或调用

### Requirement: 离线和真实双 deployment 验证诚实分层
默认 unit/contract/eval/smoke-local SHALL 使用 fake 或两个隔离 provider doubles 且零网络。真实 failover smoke artifact SHALL 使用 `model-failover-live-smoke/v1` 四分支 exact判别联合：顶层字段只允许`schema_version/status/provider_called/attempt_count/chain_id/selected_ordinal/candidates/usage/reason_code`；`status`使用封闭枚举，`provider_called`为bool，`attempt_count`为非bool非负整数，`chain_id`为nullable 64位小写十六进制，`selected_ordinal`为nullable非bool正整数。candidate字段只允许`ordinal/deployment_id/provider/model/outcome/attempt_count/not_started_proof_count/request_sent/response_observed/not_started_reason/http_status`；ordinal从1连续唯一升序，三个identity非空，顶层`attempt_count`等于逐候选`attempt_count`总和。not-called强制零count/false/null，not-started强制正attempt且proof count相等，其他outcome proof为0且`not_started_reason=null`；两类可信reason的观察组合保持唯一。`usage`只允许`input_tokens/output_tokens/cost_usd/cost_status`，token为非bool非负整数，cost status封闭为reported/estimated/unavailable，cost为有限非负number或null且null当且仅当unavailable，复用API 5.29。拒绝unknown fields、负数、bool数字、NaN/Inf、非法ordinal/count/status组合和artifact/durable evidence不一致。

真实验收探针 MUST 同时要求本会话授权、独立opt-in、两个不同`deployment_id`、两个不同`credential_ref`、两个不同受信endpoint、可受控产生任一受信not-started的首选fixture，并冻结两条route均`max_attempts=1`；两个deployment的`provider_kind` MAY相同，公共validator MUST以同kind双deployment正合同证明该前置可达，并以任一deployment/credential/endpoint复用负合同关闭失败。缺任一前置时MUST以`provider_called=false/attempt_count=0/chain_id=null/selected_ordinal=null/candidates=[]/usage=null`零调用输出`hosted-unverified`，`reason_code`按授权、opt-in、凭据对、deployment/endpoint隔离对、fixture固定优先级，并精确为`authorization_missing|failover_opt_in_missing|credential_pair_missing|deployment_pair_invalid|not_started_fixture_missing`之一。`passed`唯一形状为`provider_called=true/attempt_count=2/chain_id=<非空64位摘要>/selected_ordinal=2/reason_code=null`及恰好两个ordinal `[1,2]`：首项not-started/1 attempt/1 proof且命中两类可信组合之一，次项completed/1 attempt/0 proof、request/response均true、not-started reason为空、HTTP status为2xx且是唯一completed，usage完整；producer/validator必须从同一durable route-chain evidence逐值核对`chain_id`、两个candidate identity/outcome、全局attempts、首项proof、`selected_ordinal`与usage。

四分支联合按`status`与identity形状判别：chain冻结前本地失败只允许`status=failed/reason_code=contract_failure/provider_called=false/attempt_count=0/chain_id=null/selected_ordinal=null/candidates=[]/usage=null`；除此子变体外，`passed|external-blocked|failed`都强制`chain_id`与两个candidate非空。`provider_called`逐值复制durable invocation事实，任一request/response观察强制其为true，false则全部观察为false，response observed另蕴含request sent。external-blocked的`reason_code`只允许`network_unavailable|provider_rejected|quota_blocked|provider_timeout|provider_result_unknown`，并强制`selected_ordinal=null`且无completed；chain冻结后本地failed只允许`reason_code=contract_failure`，必须保留两个candidate与观察事实，`selected_ordinal`若非空须命中唯一completed。其他已发送、response或timeout后仍尝试次选属于本地failed/contract_failure；所有非空usage与durable evidence继续逐值一致，failed空/非空identity不得混搭；任何status使用其他reason、`passed`的reason非null或reason/status不匹配都关闭失败。

#### Scenario: 真实前置不完整
- **WHEN** 授权、opt-in、任一隔离 credential、任一受信 deployment 或受信 not-started fixture 缺失
- **THEN** smoke 输出 `status=hosted-unverified`、`provider_called=false`、`attempt_count=0` 并以进程 0/CI skipped 收口
- **AND** 不探测 secret 内容、不打开 DNS/HTTP、不消耗 token

#### Scenario: 双真实 deployment 成功切换
- **WHEN** 全部前置完整且首选以 `client_not_started` 或 `trusted_business_not_started` 产生可复核 not-started、次选完成一次文本调用
- **THEN** smoke 才输出 `passed`，并包含去敏 chain/attempt/usage/cost evidence
- **AND** 首选和次选调用事实、selected ordinal 与 reservation transition 可逐值复核

#### Scenario: 完整前置后的外部故障
- **WHEN** 全部前置完整且调用过程中发生网络、配额或 provider 故障
- **THEN** smoke 输出 `external-blocked`、进程 2/CI fail，并如实保留 provider_called 与已知 attempt 数
- **AND** 不把外部失败写成 PASS 或本地合同成功
