## ADDED Requirements

### Requirement: 模型 route-chain reservation 在同一 claim 内原子转移
Direct 与 allocation 模型调用 SHALL 使用原 stable key 和 claim 保存 route-chain state。claim 的既有 `side_effect_state` 是整条调用的单调高水位；state 中每个 candidate 另有独立单调聚合 `side_effect_state=not_started|started|unknown|result_committed` 与按全局 attempt 排序的 `not_started_proofs[]`。`result_committed` 只允许与本变更封闭定义的 terminal 组合共同出现；本阶段新增组合精确为 `state=cancelled/reason=invocation_cancelled/side_effect_state=result_committed`，不得用于 transfer、waiting、deny、unknown 或仍持有 reservation 的形状。repository 只可在 owner ledger lock/CAS 下，凭当前 candidate 覆盖从首次到末次全部实际 attempts、逐值匹配 durable attempt evidence且没有悬空 attempt的完整 proof list，按冻结 ordinal扫描后继并原子替换 current candidate、首个可预约 next candidate、reserved token/cost impact与 active ordinal。Soft review threshold 对候选明确选择有限 fallback时，repository MUST 耐久标记 `budget_ineligible/reason=soft_budget`；以候选冻结 bound替换当前 impact仍超过 current owner余额时 MUST 标记 `budget_ineligible/reason=balance`。两者均为零 provider attempt/proof/reservation/permit/client，继续扫描更后候选，且不得混同 `PolicyEngine deny|require_approval`。每条 `client_not_started` proof只证明该 attempt 的 request/HTTP response未发生；每条 `trusted_business_not_started` proof要求该 attempt、candidate与 claim高水位为 started并保留 request/HTTP response事实，由当前 endpoint policy/classifier、冻结状态白名单、无 response identity/usage/text/delta共同证明业务与计费未开始。末次 client proof不得回退早期 trusted-business造成的 candidate/claim高水位。存在未证明的 started/unknown/response attempt MUST拒绝跨 provider transfer；不得先 release再 claim，或让两个候选 reservation同时计入 parent impact。

State MUST 另外保存按全链全局 attempt 从1连续、不可删除或覆盖的 `attempt_lifecycle[]`。每次首次调用或retry在client/send/provider边界前，repository MUST 在owner lock/CAS中先追加`lifecycle_state=started`及`model-route-attempt-identity-v1` digest；identity绑定chain、usage/operation、candidate、attempt、route/endpoint/retry digest。started无论`request_sent=false|true`都视为已开始，只有与proof同一UoW的`not_started_proven`、与可信actual/final同一UoW的`settled`或保留reservation的`unknown`可关闭，终态不可降级。Transfer要求当前candidate全部lifecycle records均为`not_started_proven`且与proof一一匹配；started mark提交后send前崩溃、send后proof/settlement前崩溃或commit-ack未知都进入needs-review，不得重发、创建下一attempt或推进provider。

Chain owner UoW 的唯一顺序为candidate policy/audit、reservation、attempt started identity、Bulkhead permit、client/prepare、send/iterate；legacy单route原顺序不变。Permit/client/prepare在send前确定失败也必须保留started identity，并以同一UoW写proof、关闭lifecycle和处理reservation。

#### Scenario: direct reservation 跨候选替换
- **WHEN** direct claim 从较小候选推进到较大候选且 parent 余额足够
- **THEN** 一个事务提交新的 candidate state、claim bound、ledger impact 与 version
- **AND** 并发读取只观察 transfer 前或 transfer 后完整状态

#### Scenario: allocation reservation 跨候选替换
- **WHEN** delegation allocation 在原 ceiling 内推进候选
- **THEN** allocation、delegation remaining impact 与 parent aggregate 使用同一原子边界
- **AND** 不创建第二 allocation 或提高 delegation ceiling

#### Scenario: 中间预算不可用候选不产生占额窗口
- **WHEN** A 已可信 not-started，B 命中 `soft_budget|balance`，C 在同一 projected owner impact下可预约
- **THEN** direct/allocation UoW 同时提交 A=not_started、B=budget_ineligible、C=active和唯一 A→C reservation transition
- **AND** B 的 attempt/proof/impact为零，读者只能观察 UoW前的 A reservation或 UoW后的 C reservation

#### Scenario: 无后继可预约时原子释放并耗尽
- **WHEN** A 已可信 not-started且全部后继均 static或budget ineligible
- **THEN** 同一 UoW actual-zero结算 A、释放 current impact、保存 skip states与唯一 A→null terminal transition
- **AND** 初始无 reservation的全不可用链以空 transitions收口；两种形状均不留下退款后重选窗口

### Requirement: route-chain state 可重放且 unknown 永久围栏
新 chain claim SHALL 保存版本化、去敏的 chain id、candidate count、互斥 active/waiting-approval/selected ordinal、delta fence、current reservation、全局连续 attempt lifecycle、逐候选聚合 side-effect/request/response/completion、有序 not-started proof records 与连续 transition；candidate state精确包含零 attempt/proof/reservation的 `static_ineligible` 与 `budget_ineligible`，后者 reason精确为 `soft_budget|balance`。普通 ineligible不单独追加 transition且 approval bindings为空；获批 activation的 balance skip强制保留 request/grant两个 digest和此前 waiting coordination transition，不得出现该 ordinal的 reservation activation，继续后继只允许零 released impact的 `transferred/balance` transition。Exact attempt-start/close、retry-proof append、transfer与terminal replay MUST返回同一 durable状态；chain id、ordinal、attempt identity、lifecycle、bound、reason、proof数量/顺序/字段冲突 MUST返回 `budget.operation_conflict`。Repository在读取 current balance前 MUST先按 stable key/identity读取已提交 state：已经耐久的 `budget_ineligible`及所选 ordinal不得因余额、policy或配置后来变化而重评。调用级 claim高水位为 started、但当前 active candidate为 not_started时，只有所有前序 started candidates的全部 lifecycle records都已凭完整 proof list原子关闭为`not_started_proven`并有actual-zero transition才是合法恢复形状；“下一尚未开始的全局 attempt”唯一指对应identity record尚不存在，恢复必须先创建started record再进入provider边界。除此之外，当前 candidate存在任何started/unknown/settled非零实际record、已发送或无完整受信proof的HTTP response、attempt identity缺口/冲突，或出现任一response identity/usage/text/delta、delta fence为真时，claim与ledger MUST保留reservation/needs-review或按可信actual结算，拒绝transfer、新budget operation与terminal。

Route-chain可信actual取消是独立terminal而不是transfer：只有provider-neutral stream关闭结果证明远端`stopped`、usage `finality=complete`且所有启用维度完整，并且不存在durable delta intent或发布确认不明时，repository才可在同一owner UoW以actual替换reservation，把claim、matching lifecycle和candidate置`result_committed`，candidate置`cancelled/reason=invocation_cancelled`，清空active/waiting/selected/current reservation并保留既有transitions。相同stable key重放必须返回同一actual与state且不调用provider；其他关闭结果保持reservation/needs-review。

#### Scenario: transfer 提交确认丢失
- **WHEN** transfer 已提交但调用方未收到确认，随后以相同参数重试
- **THEN** SQLite 与 PostgreSQL 均重放已提交状态，不再次修改 ledger impact

#### Scenario: started 或 response 缺可信证明时尝试 transfer
- **WHEN** active candidate 的 side-effect state 已为 started/unknown，adapter 已发送请求或出现 HTTP response，但不存在完整 `trusted_business_not_started` proof，或已出现 response identity/usage/text/delta
- **THEN** repository 拒绝推进 ordinal并保留原 reservation
- **AND** owner ledger 保持 needs-review 或原未决状态

#### Scenario: client 前 not-started transfer
- **WHEN** active candidate 自身仍为 not_started，client/send 前 proof 逐值匹配且没有任何 response 或结果事实
- **THEN** SQLite 与 PostgreSQL 在一个事务收敛当前候选并替换为下一 reservation
- **AND** claim 高水位无论仍为 not_started，还是因前序受信业务未开始候选保持 started，都不回退；相同 proof identity 重放幂等，缺失、篡改或冲突 identity 全部关闭失败

#### Scenario: 受信业务未开始 transfer
- **WHEN** active candidate 自身与 claim 高水位都保持 started，request/HTTP response 事实为真，状态码命中冻结跨 provider 白名单，端点 classifier proof 逐值匹配且没有 response identity/usage/text/delta
- **THEN** SQLite 与 PostgreSQL 在一个事务以 actual zero 收敛当前候选并替换下一 reservation，不回退 claim 的历史事实
- **AND** proof/status/classifier 任一缺失、篡改或 replay 冲突都保留原 reservation并关闭失败

#### Scenario: 同候选 retry proof 追加与恢复
- **WHEN** 一个 direct 或 allocation candidate 连续产生两次受信 not-started，第一条 proof 提交后继续同 route retry，第二条 proof 提交后才 transfer
- **THEN** SQLite 与 PostgreSQL 在每次下一 attempt 前原子追加全局连续且不可覆盖的 proof record，并让 attempt evidence、candidate 聚合高水位和 owner impact 逐值一致
- **AND** proof append 或 transfer 的 commit-ack 丢失只重放同一 record/state；缺失、覆盖、重排、冲突或悬空 attempt 保留 reservation并且 provider/后继调用次数不增加

#### Scenario: retry started identity 的两个崩溃窗口
- **WHEN** attempt 1 proof已提交，attempt 2 started mark提交后分别在send前崩溃，或在send后、proof/settlement前崩溃
- **THEN** direct/allocation在SQLite与PostgreSQL都恢复同一attempt 2 identity与current reservation，并进入needs-review或补投同一settlement
- **AND** 两个窗口均不重发attempt 2、不创建attempt 3、不transfer；identity冲突或lifecycle非法降级返回`budget.operation_conflict`

#### Scenario: 可信actual取消原子释放reservation并可重放
- **WHEN** active stream attempt的关闭结果证明`stopped + complete usage`且无durable delta intent或发布确认不明
- **THEN** direct/allocation在SQLite与PostgreSQL同一UoW按actual更新owner impact，保存`cancelled/invocation_cancelled`与`settled/result_committed`，并清空active/waiting/selected/current reservation
- **AND** 相同stable key重放返回同一actual、state与既有transition数组，不重复释放、不调用当前或后继provider；不完整/unknown关闭结果保留原reservation并needs-review

#### Scenario: 两种 proof 的三候选混合顺序可重放
- **WHEN** direct 或 allocation chain 依次经历 `trusted_business_not_started → client_not_started → active`，或 `client_not_started → trusted_business_not_started → active`
- **THEN** SQLite 与 PostgreSQL 的 claim 高水位、三份 candidate side-effect/request/response/proof、reservation 与连续 transition 逐值一致
- **AND** 任一 transfer 前、commit-ack 丢失后或进程恢复时都只返回同一 active ordinal，不重复扣款、不回退 started、不重放前序 provider

#### Scenario: budget-ineligible 决定在 crash recovery 中不漂移
- **WHEN** B 的 balance判定与 A→C direct transfer已提交但确认丢失，随后 owner余额发生变化并恢复相同 stable key
- **THEN** SQLite 与 PostgreSQL 均先重放 B=budget_ineligible、C=active及原 ledger version结果，不重新选择 B
- **AND** 若 terminal exhaustion已提交也只补投同一 failure/outbox，不重新预约或调用任何 provider

### Requirement: 审批等待与恢复不占预算且可重放
Route-chain invocation SHALL 在任何 policy/coordination row前从受信上下文与原始语义 operation key冻结唯一 `usage_call_id` 和 `operation_identity_digest`。首候选 require approval 时，repository MUST 用该身份在同一 owner lock/shared-budget UoW 建立 token/cost impact 均为 0、`side_effect_state=not_started` 的 coordination claim/allocation并保存完整 v1 chain state；它不构成 model reservation。后继候选 require approval 时，repository SHALL 在同一 UoW 释放前一 reservation、把 parent impact 置零、写入 waiting ordinal 与绑定同一 usage/operation identity 的 `model-route-chain-approval-request-v1` digest；不得提前预约目标候选。此时 approval id/lease id 尚不存在，grant digest必须为空。ApprovalService 在独立 approval UoW 创建绑定同一身份的 record、独占 claim resolution lease并构造 matching grant；invocation 从私有 checkpoint重算并校验 usage identity、active lease和 request digest后，shared-budget UoW 才可用 waiting ordinal/request digest/usage call id CAS并重检 current owner balance。余额足够时原子保存 `model-route-chain-approval-grant-v1` digest、把 coordination/waiting row替换为目标 reservation并切换 active ordinal；余额不足时保存同一 grant digest和 `budget_ineligible/balance`、保持零 impact，后继必须重新执行自己的 Policy/HITL。shared-budget repository 不消费或完成 approval lease；两个 UoW 以 frozen usage identity、durable digest、approval lease fencing和幂等恢复衔接。activation 或 balance-skip提交确认丢失后的 exact replay MUST 返回同一 claim、grant digest、candidate state与 reservation；MUST NOT 用 legacy `approved:<approval_id>` rekey、新建映射或第二 claim。usage identity、stale、mismatch、takeover 后旧 lease或不同 grant任一不匹配 MUST 保持零新增 impact、零 provider call并关闭失败。首候选 policy deny 不创建 row；后继 deny MUST 原子结算前序已知 not-started attempt 的零 charge、释放 impact、写 rejected failure evidence并以 `model.policy_denied` 终止，不得进入 exhausted causes。

#### Scenario: require approval 暂停
- **WHEN** 当前候选以 `client_not_started|trusted_business_not_started` 安全收敛且下一候选 policy 要求审批
- **THEN** direct/allocation claim 进入 waiting approval，current reservation 与 parent impact 都为零
- **AND** provider permit/client 仍未取得

#### Scenario: 首候选审批具有零 impact 耐久载体
- **WHEN** 第一候选在任何 reservation/provider 副作用前要求审批
- **THEN** direct/allocation repository 用首次冻结的 usage call id写入完整 chain state 与 request binding digest，但 grant binding、reserved token/cost 和 parent impact 均为零
- **AND** recovery 用私有 checkpoint、approval artifact与冻结 root snapshot重算同一 usage/operation identity和 chain，当前配置变化不影响 continuation

#### Scenario: approval handoff 与 activation commit-ack 丢失
- **WHEN** approval lease 已在独立 UoW 提交，随后目标 reservation/grant digest activation 已提交但调用方未收到确认
- **THEN** 首次提交只追加同ordinal的`approved/approval_granted` transition，released精确为`0/null`、reserved精确为目标冻结bound，candidate在同一UoW从waiting直接变为active且不追加第二条`activated` transition
- **AND** exact replay 以首次 usage call id、相同 request/grant digest返回同一 claim、active ordinal、bound与完整transition数组，不重复预算影响，也不声称在 shared-budget UoW 消费 lease
- **AND** lease claim 后、activation 前崩溃保持零 provider；伪造 usage identity、不同 lease/grant、takeover旧lease或任一from/to/reason/bound非法组合关闭失败且不rekey

#### Scenario: approved activation 余额不足
- **WHEN** matching grant已提交，但目标候选在 shared-budget activation UoW中因 current balance不足无法预约
- **THEN** direct/allocation row耐久保存该 ordinal的 `budget_ineligible/balance`与 grant digest，impact和 provider调用均为零
- **AND** 不追加`approved|activated` transition；该ordinal成为零impact source anchor，后继独立policy的allow/require-approval/deny或全耗尽分别只允许canonical `transferred/balance`、`waiting_approval/approval_required`、`terminated/policy_denied`、`terminated/route_exhausted`，所有released/reserved按source是否有reservation逐值为零或目标bound
- **AND** 相同恢复不因余额变化重试获批候选，不把grant复用于后继，也不让中间普通skip改变source anchor

#### Scenario: 初始前导 skip 不伪造 reservation source
- **WHEN** 初始ordered scan先产生一个或多个零impact static/budget-ineligible candidate，再由非首ordinal返回require-approval或deny
- **THEN** require-approval只允许`from_ordinal=null/to_ordinal=目标/state=waiting_approval/reason=approval_required/released=zero/reserved=zero`，deny不追加transition且以目标denied state收口
- **AND** direct/allocation、SQLite/PostgreSQL及commit-ack recovery逐值一致，拒绝用前导skip ordinal或不存在的current bound编码source

### Requirement: migration 保留旧单 route 数据
Migration `0017_model_route_chain_state` SHALL 仅增加 nullable route-chain state 列，不回填或重写既有 0016 claim/allocation identity、金额、状态和 result。旧 row 为 null 时 MUST 继续按单 route replay；新 v1 state必须经 DTO/repository exact-shape 验证。SQLite/PostgreSQL downgrade 在两表均无 non-null state 时 MAY 删除列；只要存在 completed、cancelled、active 或 needs-review 任一 non-null state，MUST 在 DDL 前以 `storage.route_chain_state_present` 关闭失败并逐值保留数据与 ledger impact。不得通过清空或导出后删除证据绕过该门禁。

#### Scenario: 0016 数据升级后重放
- **WHEN** 含 settled、reserved 与 needs-review 旧单 route row 的数据库升级到 0017
- **THEN** 所有旧字段逐值不变且 route-chain state 为 null
- **AND** 既有 replay/terminal fencing 行为不变

#### Scenario: non-null chain state 阻止降级
- **WHEN** direct 或 allocation 表存在 completed、cancelled、active 或 needs-review route-chain state
- **THEN** SQLite 与 PostgreSQL downgrade 均返回 `storage.route_chain_state_present`
- **AND** migration 版本、列、row、reservation 与 ledger impact 全部保持不变

#### Scenario: 仅 null 旧数据允许降级
- **WHEN** 两张表所有 route-chain state 均为 null
- **THEN** downgrade 可移除新增列，0016 旧 row 逐值不变

## MODIFIED Requirements

### Requirement: 所有 budget operation 的 stable key 与 immutable identity 分离
Direct stable key SHALL固定为`(tenant_id,budget_owner_run_id,usage_call_id)`；delegation top-level stable key SHALL固定为`(tenant_id,budget_owner_run_id,delegation_claim_id)`；delegation child allocation stable key SHALL固定为`(tenant_id,budget_owner_run_id,delegation_claim_id,usage_call_id)`。本requirement中的`delegation_claim_id` MUST逐值等于`0015 AgentDelegation.id`，在`budget_operation_claims`与`delegation_budget_allocations`中的物理列名 MUST为`delegation_id`；系统 MUST NOT生成或接受第二套delegation claim标识。因此规范中的`delegation_claim_id` key与storage delta中的`delegation_id` key是同一个key。三类key都不得包含动态余额、reservation结果或event capacity。每个direct、delegation top-level claim与allocation MUST另外持久化`identity_schema_version`、immutable identity hash、tenant-scoped opaque request fingerprint及key version和对应非敏感关联字段。

Legacy单route与embedding的direct/allocation identity MUST逐字保持`identity_schema_version=budget-operation-v1`及其既有canonical hash，不得携带route-chain字段。v1在最终actual route与trusted intent确定后、任何shared reservation/event-capacity mutation或provider副作用前，仍只对以下封闭字段生成：`ownership_kind=direct|allocation`、`run_id`、`agent_id`、allocation时非空的`delegation_claim_id`、`usage_kind=model|embedding`、稳定语义operation slot、tenant-scoped keyed request fingerprint及key version、owner tree snapshot ID、适用agent sub-snapshot ID、provider/model、price source ref/version、embedding cache-key digest（model时为null）、cost-enabled状态与各启用维度trusted bound。

显式route chain MUST使用`identity_schema_version=budget-operation-v2`；v2在最终chain与trusted intent确定后、任何shared reservation/event-capacity mutation或provider副作用前，由可信runtime对以下封闭字段生成：`ownership_kind=direct|allocation`、`run_id`、`agent_id`、allocation时非空的`delegation_claim_id`、`usage_kind=model`、稳定语义operation slot、tenant-scoped keyed request fingerprint及key version、owner tree snapshot ID、适用agent sub-snapshot ID、冻结chain ordinal 1 candidate的provider/model、price source ref/version、`embedding_cache_key_digest=null`、cost-enabled状态与各启用维度trusted bound，以及非空64位小写十六进制`route_chain_digest`和逐值等于冻结candidate count的正整数`route_candidate_count`。`route_chain_digest` MUST逐值等于冻结`ModelRouteChainPlan.chain_id`；完整Agent/request授权及所有candidate route/price/bound MUST同时进入semantic request fingerprint与该chain digest。ordinal 1兼容投影不得随active/selected ordinal、预算选择或恢复时current balance变化；v2不得省略新增字段或降格为v1，v1不得夹带v2字段。

Delegation top-level identity MUST在`0015`按`(tenant,parent,idempotency_key)`与normalized request hash唯一定位或准备创建relation之后、任何`0016`claim/reservation、event-capacity、child或queue副作用之前生成。其版本化canonical payload MUST封闭包含`ownership_kind=delegation`、parent `run_id`、source/target `agent_id`、`delegation_claim_id`、`usage_kind=delegation`、`operation_slot=idempotency_key`、对`0015`同一normalized request canonical bytes生成的tenant-scoped keyed fingerprint及key version、owner tree snapshot ID、target agent sub-snapshot ID、target frozen route/price catalog digest、cost-enabled状态与本次可信top-level token/cost reservation bound。Top-level claim本身不调用provider，因此provider/model/单一price source/cache-key字段 MUST固定为null；target catalog digest MUST覆盖该target封闭允许routes及每条route的price refs/versions和必需price值，不能用null字段跳过route/price绑定。`0015` request hash继续证明请求幂等，`0016` identity另外证明budget replay context；两者都必须一致才是exact replay。Delegation top-level仍只使用既有版本，不接受route-chain v2字段。

三类request fingerprint MUST由versioned tenant-scoped key对各自canonical semantic request bytes生成；数据库只保存opaque fingerprint与key version，不保存key、child input、prompt或embedding原文。Identity canonical JSON MUST使用UTF-8、排序键、紧凑分隔符并拒绝NaN/Infinity，随后以固定hash算法生成持久化hash。动态current balance、event capacity、approval result、cache hit/miss结果、provider result、latency和错误不得进入identity。Cache lookup的稳定cache-key digest进入legacy embedding usage identity，但hit/miss由首次原子提交的durable result决定：提交前失败可重做只读lookup，提交后同identity必须重放首次结果。相同stable key只有对应`identity_schema_version`与identity hash逐值相同才是exact replay；v1/v2跨版本、任一封闭字段、fingerprint或版本不同 MUST在owner/relation mutation、delegation子额度、parent budget、capacity检查及外部副作用前返回内部`budget.operation_conflict`。Direct seam保持该内部code；allocation冲突若向parent delegation结果传播 MUST封闭映射为既有`delegation.execution_failed`。Delegation top-level replay MUST先验证`0015` normalized request hash，再验证`0016` identity；request hash异值或同hash但identity异值都公开映射既有`delegation.idempotency_conflict`，内部MAY记录不含fingerprint、snapshot内容、route/price或动态数值的`budget.operation_conflict` evidence。Insert/unique race MUST回滚后重读并应用相同判定。

#### Scenario: 相同 usage_call_id 的不同请求发生 identity conflict
- **WHEN** 同一tenant/owner/`usage_call_id`以不同request fingerprint、usage kind、actual route、snapshot/sub-snapshot、price version、trusted bound、identity schema version或route-chain digest/count重试
- **THEN** direct在读取current balance或event capacity前返回`budget.operation_conflict`，不重放旧result、不新增claim且provider调用为零；错误不得公开fingerprint或identity字段

#### Scenario: Child allocation 同 key 异 identity 在子额度前冲突
- **WHEN** 同一tenant/owner/delegation claim/`usage_call_id`以不同child request fingerprint、usage kind、actual route、target sub-snapshot、price version、trusted bound、identity schema version或route-chain digest/count重试
- **THEN** allocation在relation、子额度、parent balance与event capacity检查前返回内部`budget.operation_conflict`，不重放旧result、不新增allocation且provider调用为零；若parent delegation收口该失败，对外只使用`delegation.execution_failed`

#### Scenario: Delegation 同 request hash 但 budget identity 不同仍冲突
- **WHEN** 同一tenant/owner/idempotency key命中相同`0015` normalized request hash，但重试使用不同fingerprint key version、tree/target sub-snapshot、target route/price catalog digest或trusted top-level bound
- **THEN** 系统在读取current balance、写relation/claim/capacity或创建child/queue前返回`delegation.idempotency_conflict`，内部可记脱敏`budget.operation_conflict`；不得仅凭旧request hash exact replay

#### Scenario: Delegation top-level exact replay 不重算动态余额
- **WHEN** delegation stable key、`0015` request hash与`0016` top-level identity逐值相同，但其他operation已改变current balance或event capacity
- **THEN** 系统复用首次durable top-level claim/reservation/result，不重新预约或派生新identity，不创建第二个relation/child/queue，也不因当前余额变化返回新budget错误

#### Scenario: Cache 状态变化不改写已提交 identity
- **WHEN** 相同embedding stable key、request fingerprint、cache-key digest与route identity首次提交cache hit或miss结果，随后cache内容发生变化并重试
- **THEN** 系统按相同identity重放首次durable result；hit/miss不进入identity且不得借重试切换到另一种provider副作用路径

#### Scenario: Route chain identity 不随余额选择漂移
- **WHEN** 相同显式chain stable key以逐值相同的v2 identity重试，但current balance变化会使重新规划选择不同active candidate
- **THEN** repository先重放首次durable chain state与reservation结果，不以新active candidate重写ordinal 1兼容投影、identity hash或route-chain digest，也不再次调用provider
