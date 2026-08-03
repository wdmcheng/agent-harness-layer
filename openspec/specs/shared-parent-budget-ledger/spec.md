# shared-parent-budget-ledger Specification

## Purpose
TBD - created by archiving change shared-parent-budget-ledger. Update Purpose after archive.
## Requirements
### Requirement: Parent execution tree 共用 token 与 cost 硬上限
系统 SHALL 将 root run 的 `max_tokens_per_run` 与 `max_cost_usd_per_run` 作为整个 parent execution tree 的共享硬上限。顶层 direct model、direct embedding 与 delegation claim MUST 在同一 parent ledger 和同一 row lock/CAS 下竞争；token 占用 MUST 等于可计费 input 与 output 之和，embedding 只计 input。Token维度始终启用；cost维度仅在`max_cost_usd_per_run`非null时启用。任一已启用维度的未知实际值 MUST使用既有reservation保守占用，不得按零；关闭维度不参与余额、越界或unknown fencing。

Ledger MUST 以 `(tenant_id,budget_owner_run_id)` 为唯一键；`budget_owner_run_id` MUST 非空、tenant-fenced 且引用 execution-tree root 的 `AgentRun.run_id`。Root operation 的 owner MUST 是自身 `run_id`；delegation 顶层 claim 与其 child allocation MUST 通过 tenant-fenced 唯一 delegation relation 解析到同一 root owner。系统 MUST NOT 复用 nullable `AgentRun.parent_run_id` 作为 ledger owner，也 MUST NOT 以 `(tenant_id,null)` 聚合同租户 root runs。P0 只允许单层 delegation；发现嵌套、跨 tenant、缺失、冲突或多重 relation 时 MUST 在 ledger mutation 与外部副作用前 fail closed。

#### Scenario: Delegation 后的 direct 调用不能超支
- **WHEN** parent token 上限为 100、已结算 direct usage 为 10、active delegation reservation 为 60，新的 direct operation 最坏情况需要 50
- **THEN** shared ledger 在 provider 副作用前拒绝该 direct operation，既有合计占用保持 70，provider 调用次数为零

#### Scenario: Direct 后的 delegation 竞争同一余额
- **WHEN** active direct reservation 已占用 parent 的部分 token 或 cost，随后申请新的 delegation
- **THEN** delegation claim 在同一 ledger 锁/CAS 下扣除该 direct reservation，只能在 token 与 cost 两个启用维度都不越界时提交

#### Scenario: 同一 tenant 的独立 root ledger 隔离
- **WHEN** 同一 tenant 创建两个 `parent_run_id=null` 的独立 root runs，并分别执行 direct 或 delegation operation
- **THEN** 两者的 `budget_owner_run_id` 分别等于各自 `run_id`，各自只竞争自己的 ledger，不共享余额、reservation、needs_review 或 terminal fencing

#### Scenario: 同一 root 的 direct 与 delegation 命中同一 owner
- **WHEN** root run 先建立 direct claim，随后以该 root 为 parent 建立 delegation claim和child allocation
- **THEN** 三者的非空 `budget_owner_run_id` 都等于该 root `run_id`，并在同一 owner row lock/CAS 下竞争和结算

### Requirement: 外部副作用前建立受信最坏情况 reservation
每个全新的顶层 direct 或 delegation operation MUST 在 provider、child、queue 或业务执行副作用前，从受信 router/adapter/descriptor/policy 边界为每个已启用维度计算有限的最坏 reservation。调用方、prompt、child input 或 HTTP payload MUST NOT 自报、覆盖或缩小该值；任一已启用维度无法得到有限可信上界时 MUST fail closed。Cost维度关闭时不要求cost reservation或price上界。两个direct claim、两个delegation claim及其混合并发 MUST使用同一原子竞争边界。

#### Scenario: 混合并发只有安全组合提交
- **WHEN** direct 与 delegation 对同一 parent 真并发，二者分别低于读取时余额但合计会越过 token 或 cost 任一上限
- **THEN** SQLite 与 PostgreSQL 都只提交不越界的 reservation 组合，被拒绝 operation 没有 provider、child 或 queue 副作用

#### Scenario: 无可信 cost 上界时拒绝
- **WHEN** parent 配置非 null cost 硬上限，但所选 provider/模型没有受信且有限的最坏 cost 上界
- **THEN** operation 在 provider 或 child 副作用前稳定拒绝，不以 0、历史均值或调用方估计替代上界

### Requirement: Direct 硬预算拒绝使用封闭且不泄露余额的错误语义
Direct model/embedding 因 `intent_unbounded`、`hard_limit_ineligible`、`balance_insufficient`、`snapshot_invalid` 或 `ledger_needs_review` 被拒绝时，module/runtime 与 usage rejection evidence 的稳定错误码 SHALL 统一为 `budget.reservation_rejected`。细分 reason MAY 写入唯一、脱敏的内部 decision/audit evidence，但 MUST NOT 进入公开 response，也不得包含 hard limit、当前余额、reservation、price 或 owner 内部值。Local/SQLite 与 service/PostgreSQL/Redis MUST 逐值返回相同 code；model 与 embedding MUST NOT 自行改名。Delegation 的无可信上界、静态超限或当前余额不足 SHALL 继续映射既有 `delegation.budget_exceeded`，不得把 direct code 混入既有封闭 delegation 错误集。

#### Scenario: 三类 direct 拒绝共享稳定 code
- **WHEN** direct model 或 embedding 分别因启用维度无可信有限上界、trusted intent 静态越过 frozen hard limit，或原子 reservation 时当前余额不足而拒绝
- **THEN** 三条路径的公开/module错误与 usage rejection evidence `error_code` 都为 `budget.reservation_rejected`，provider 调用为零；内部 reason 可区分 `intent_unbounded|hard_limit_ineligible|balance_insufficient`，但不公开动态数值

#### Scenario: Snapshot 或 needs-review 拒绝不形成余额 oracle
- **WHEN** owner snapshot 无效或 ledger 已处于 needs-review，direct operation 被 fail closed
- **THEN** 调用方仍只得到 `budget.reservation_rejected`，内部 evidence 分别记录脱敏 `snapshot_invalid|ledger_needs_review`，不同入口不得用错误码或消息泄露 owner、余额或价格

### Requirement: 组合 UoW 使用确定性检查与错误优先级
Direct 与 delegation 的全新 claim SHALL 在 SQLite/local 与 PostgreSQL/service 按相同顺序执行检查。第一优先级是 tenant-scoped stable key 与 immutable replay identity：完全一致的既有 operation MUST 直接重放首次 durable state/result，不重新检查当前余额或 event capacity；同 key 异 identity 的 direct MUST 返回 `budget.operation_conflict`，delegation MUST 返回既有 `delegation.idempotency_conflict`。第二优先级是授权、owner、relation、snapshot 与持久化状态完整性：direct 映射 `budget.reservation_rejected` 与脱敏内部 `snapshot_invalid`；delegation 的跨 tenant/ownership 映射 `delegation.policy_denied`，durable relation/snapshot 损坏映射 `delegation.execution_failed`。第三优先级是 `event.sequence_state_invalid`。第四优先级是 hard-budget eligibility/current balance：direct 返回 `budget.reservation_rejected`，delegation 返回 `delegation.budget_exceeded`。第五优先级是 event capacity exhaustion，MUST 保留前置合同的 `event.sequence_exhausted`，不得改写为 `budget.*` 或 `delegation.*`。Insert/unique race MUST 回滚并重新读取 stable key，再按第一优先级判为 exact replay 或 identity conflict，不得暴露数据库异常。

#### Scenario: Budget 与 capacity 同时不足时 budget 优先
- **WHEN** 全新 direct 或 delegation operation 同时会越过 shared hard budget 且 event capacity 也不足，且没有更高优先级的 replay/integrity/state-invalid 失败
- **THEN** direct 返回 `budget.reservation_rejected`，delegation 返回 `delegation.budget_exceeded`；组合 UoW 全部回滚，local/service不得因SQL检查顺序不同返回`event.sequence_exhausted`

#### Scenario: Capacity-only 失败保留前置错误码
- **WHEN** operation 的 replay identity、owner/relation/snapshot、event state与hard budget均合法，但 event capacity 不足
- **THEN** direct 与 delegation 都返回 `event.sequence_exhausted`，不建立shared claim、usage/delegation operation或任何外部副作用；该内部cross-cutting code不扩张既有`delegation.*`封闭集合

#### Scenario: Embedding sequence state corruption 先于 budget 拒绝
- **WHEN** 全新 embedding miss 的 event sequence state 已非法，且同一 operation 的 trusted intent 同时会被 shared hard budget 拒绝
- **THEN** local/SQLite 与 service/PostgreSQL 都先返回 `event.sequence_state_invalid`，不建立 claim/outbox/capacity reservation且 provider 调用为零；不得因 invocation settlement 的 budget precheck 把该错误覆盖成 `budget.reservation_rejected`

#### Scenario: Exact replay 不受当前余额或容量变化影响
- **WHEN** stable key与immutable identity精确命中已提交operation，但当前budget余额或event capacity已被其他operation改变
- **THEN** 系统重放首次durable state/result，不重新预约、不返回新的budget/capacity错误；若identity不一致则direct返回`budget.operation_conflict`、delegation返回`delegation.idempotency_conflict`

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

### Requirement: 软策略与硬账本按固定顺序执行
`policy review threshold` SHALL只作为软策略层，MUST NOT与shared hard limit合并或被解释为审批可覆盖的额度。系统 MUST在frozen config内先完成context/route降级和trusted intent；无finite bound或intent静态越过hard limit时直接拒绝。Hard-eligible intent再执行软threshold策略；fallback回到route步骤且有限终止，approval不持有额度。Allow/approved后 MUST执行shared-ledger原子reservation并重检当前余额，成功后才允许外部副作用。

#### Scenario: Approved operation 在余额变化后硬拒绝
- **WHEN** operation通过soft threshold审批，但resume时active direct/delegation impact已占满parent余额
- **THEN** 原子reservation失败且外部副作用为零，approval不得覆盖hard limit或把其他claim释放

### Requirement: Parent hard-limit snapshot 对在途 tree 不可变
Root run SHALL在创建且任何业务副作用前冻结tree snapshot：owner envelope保存hard limits、cost-enabled状态、registry/config/catalog versions与snapshot ID；root及当时显式允许的P0 target agents分别保存各自descriptor/model-policy/target-budget/route/price sub-snapshot。Child SHALL继承同一owner snapshot ID与hard limits，并按自身target `agent_id`选择对应sub-snapshot，不得把source descriptor当作target配置。Target ceiling只可收紧owner已启用维度，不能提高owner hard limit或重新启用owner已关闭的cost维度。Reload MUST只影响新root run。Fallback SHALL在当前agent对应的frozen sub-snapshot内按actual route重算trusted reservation，但 MUST NOT改变hard limits。Target、route或price ref未被冻结时 MUST在reservation与外部副作用前fail closed。

#### Scenario: Config reload 只影响新 root run
- **WHEN** 已有root run处于active/approval等待状态并发生registry/provider/budget/price reload
- **THEN** 既有tree继续使用原snapshot，approval resume与fallback也不能采用reload后limit或price；新root run使用新snapshot

#### Scenario: Child 使用 target 独立策略但共用 owner hard limit
- **WHEN** 冻结tree catalog中的target agent具有不同于source的descriptor、model policy与更严格budget ceiling
- **THEN** child按target sub-snapshot计算route/trusted bound，同时受target ceiling与owner剩余shared hard limit的更严约束；target独立策略不得创建第二个owner ledger或提高parent额度

### Requirement: 实际 usage 原子替换 reservation
可信、非 bool、非负且有限的实际 usage SHALL 在同一 ledger 原子边界按已启用维度把原 reservation impact 替换为 settled impact，MUST NOT先释放再另记实际值。只有可证明没有发生外部副作用的确定性失败才可幂等标记`released`。副作用结果未知或任一已启用维度actual不可得时 MUST保持该维reservation；trusted actual越过reservation时impact MUST提升为`max(original_reservation,trusted_actual)`。这些已启用维度异常 MUST进入`needs_review`，并在协调完成前拒绝该owner的全部新budget operation、阻止terminal，不得增加可用余额。

所有usage数值与`cost_usd/cost_status`组合无论维度是否启用都 MUST执行前置`model-usage-evidence`校验；bool、负数、NaN、Infinity或不一致组合仍属非法并 MUST拒绝。Cost维度关闭且组合合法时，`cost_usd=null,cost_status=unavailable` SHALL表示该维不适用，cost impact为0且 MUST NOT单独触发reserved/needs_review；合法reported/estimated cost MAY保留在usage/detail evidence中，但 MUST NOT参与该owner的cost余额或越界。Token等已启用维度仍须独立可信结算。

每个会产生外部副作用的新operation MUST 在调用provider、创建child或投递queue前，把原claim的durable `side_effect_state` 从`not_started`推进为`started`；该推进本身不得触发外部调用。可信result、direct/allocation settlement、delegation top-level delta、parent aggregate与`side_effect_state=result_committed` MUST在一个UoW全部提交或回滚。新writer MUST NOT产生“result已durable但ledger未settled”状态；旧`0014`/`0015`遗留半状态只能在`0016`migration预检/backfill事务中按legacy矩阵处理，运行时不得把它当作新operation恢复窗口。

#### Scenario: 结果与 ledger 已提交但 event 未发布
- **WHEN** provider 或 child 的可信结果与全部 shared settlement 已在同一 UoW 提交，但进程在最终 usage/delegation event 发布前退出
- **THEN** recovery 只从既有 outbox 补投 event，不修改 ledger、不重复调用 provider、不创建 child或投递queue

#### Scenario: 外部结果未知时不释放
- **WHEN** durable `side_effect_state=started`，但没有与shared settlement同UoW提交的可信最终result
- **THEN** claim 保持 reserved 或 needs_review、该 parent 的全部新 budget operation 与 terminal 被阻止，重试不得把未知 usage 当 0 或重新建立第二份 reservation

#### Scenario: Actual 超 reservation 提升 impact 并封锁新 claim
- **WHEN** parent 上限为 100，operation 原 reservation 为 60，可信 actual 为 80，随后申请新的 30 reservation
- **THEN** ledger 把该 operation impact 提升为 80 并进入 needs_review，在协调完成前拒绝新的 30 claim，真实与账面占用都不得被低估

#### Scenario: Cost 关闭时 unavailable 不阻止 token 结算
- **WHEN** `max_cost_usd_per_run=null`，普通model、embedding miss或delegated child返回可信token actual与合法`cost_usd=null,cost_status=unavailable`
- **THEN** operation按token维度原子settled，cost impact为0，cost unavailable不单独触发needs_review且不阻止新operation或terminal；恢复复用同一settlement

#### Scenario: Cost 关闭仍拒绝非法 cost evidence
- **WHEN** `max_cost_usd_per_run=null`，provider返回bool、负数、非有限cost或与`cost_status`不一致的组合
- **THEN** evidence在持久化/聚合前被结构化拒绝，不能因cost维度关闭而把非法值当作unavailable或零

### Requirement: Embedding cache hit 使用稳定零 impact 结算
只有tenant-fenced、纯本地且只读的embedding cache lookup MAY位于shared-budget reservation之前。`cache_status=hit`且`provider_called=false` SHALL证明本次operation没有provider token/cost副作用；其`null` token/cost与`cost_status=unavailable`表示provider usage不适用，MUST NOT解释为外部结果unknown。系统 MUST按operation ownership建立唯一零impact结算：root/direct hit由稳定`(tenant_id,budget_owner_run_id,usage_call_id)`建立或复用`state=settled`、token/cost impact均为0的direct claim；delegated child hit由稳定`(tenant_id,budget_owner_run_id,delegation_claim_id,usage_call_id)`只建立或复用`state=settled`、token/cost impact均为0的delegation allocation，MUST NOT建立parent顶层direct claim。两种路径都继续发布前置`model-usage-evidence`合同要求的cache-hit evidence。Zero-impact claim/allocation MUST NOT阻止新budget operation或terminal。Cache miss MUST在provider调用前按相同ownership取得actual route的shared reservation或delegation allocation。

#### Scenario: Cache hit 零 impact 且保留调用 evidence
- **WHEN** root/direct tenant-fenced embedding lookup确定命中且provider未调用
- **THEN** 系统以稳定`usage_call_id`原子建立或复用settled/zero-impact direct claim，usage evidence保持token/cost null、`cost_status=unavailable`与`provider_called=false`，不占用parent余额且不触发unknown/needs_review fencing

#### Scenario: Delegated child cache hit 只建立 zero allocation
- **WHEN** delegated child在既有delegation claim下执行tenant-fenced embedding lookup并确定hit、provider未调用
- **THEN** 系统以稳定`(delegation_claim_id,usage_call_id)`建立或复用settled/zero-impact allocation，不建立parent direct claim；该allocation在delegation budget aggregate中作为已知0结算，不算unknown、不增加顶层impact且不双计

#### Scenario: Cache hit 恢复不调用 provider
- **WHEN** root/direct或delegated child cache hit已确定，进程在zero-impact claim/allocation、usage result/outbox与event-capacity结算的原子UoW提交前或提交后退出，并以相同ownership key与`usage_call_id`恢复
- **THEN** 提交前全部记录均不存在，可安全重做纯读lookup；提交后全部settled记录同时存在，recovery只补投event。系统不得观察或补齐单边claim/allocation/evidence，不调用provider，也不改变parent可用余额或terminal fencing

#### Scenario: Cache miss 先预约再调用 provider
- **WHEN** tenant-fenced lookup 确定 miss
- **THEN** root/direct在provider调用前取得可信有限shared reservation，delegated child在既有delegation下取得可信有限allocation；预约失败时provider调用次数为零

### Requirement: Child usage 只在 delegation reservation 内分配
delegated child 的 model/embedding operation SHALL在既有delegation reservation下按已启用维度取得token/cost allocation；所有新allocation的reservation合计 MUST NOT超过delegation reservation。Active/unknown/invalid allocation的conservative impact MUST按已启用维度使用原reservation，可信actual不超过reservation时使用actual，可信actual超reservation时使用actual并把allocation标为needs_review。关闭的cost维度不建立cost reservation，合法unavailable不计为unknown allocation。Delegation active或任一已启用维度allocation unknown/invalid/needs_review时，系统 MUST在同一parent row lock/UoW中把顶层delegation claim impact保持为`max(original_delegation_reservation,sum(conservative_allocation_impacts),trusted_delegation_aggregate_actual)`。只有全部child allocation与delegation terminal result在已启用维度都可信、逐值一致且无needs_review时，顶层claim才 MUST原子转为settled，impact=trusted actual aggregate并归还reservation差额。任一allocation needs_review或已启用维度actual合计越过delegation ceiling时，顶层claim与parent ledger MUST同步needs_review并封锁新budget operation与terminal。Parent aggregate MUST只应用顶层claim的impact差额，allocation MUST NOT单独累加，也不得把相同child usage作为parent顶层direct claim计费。

#### Scenario: Child usage 不双计
- **WHEN** child 在一个 reserved delegation 内完成多个 model/embedding operation
- **THEN** 每个 `usage_call_id` 只结算其 delegation allocation，parent ledger 最终以 delegation settled impact 替换原 reservation，同一 usage 不出现第二笔 direct charge

#### Scenario: Child allocation 超出子额度
- **WHEN** 新 child operation 的最坏情况 allocation 会越过 delegation token 或 cost 剩余子额度
- **THEN** operation 在 provider 副作用前拒绝，parent 顶层 reservation 不增加，既有 delegation claim 保持可恢复

#### Scenario: 单个 child actual 超 allocation 原子向 parent 传播
- **WHEN** delegation 原 reservation 为 60，child allocation reservation 为 20，但可信 child actual 为 30
- **THEN** allocation impact 变为 30/needs_review；同一 parent UoW 把顶层 delegation impact 更新为 `max(60,sum(conservative allocations),trusted aggregate)` 并同步 needs_review，parent aggregate只应用顶层 claim 差额且不另加 30

#### Scenario: 多 child 累计 actual 超 delegation ceiling
- **WHEN** 多个 child allocation 分别结算可信 actual，累计 conservative impact 越过原 delegation reservation
- **THEN** 最后一次结算在同一 parent lock/UoW 提升顶层 delegation impact到该合计、标记 needs_review并封锁新 budget operation与terminal，不丢失 actual、不违反幂等且不双计

#### Scenario: 全部 child settled-under 后归还 reservation 差额
- **WHEN** delegation reservation为60，全部child allocation与terminal aggregate均可信且逐值一致，actual合计为40，没有unknown、invalid或needs_review
- **THEN** 同一parent UoW把顶层delegation claim原子转为settled、impact从60替换为40，parent aggregate只减少顶层claim的20差额且不另加allocation

### Requirement: Stable claim 关联保证幂等恢复与 terminal fencing
direct claim MUST由`(tenant_id,budget_owner_run_id,usage_call_id)`唯一关联；delegation top-level claim MUST由`(tenant_id,budget_owner_run_id,delegation_claim_id)`唯一关联并同时保存既有稳定idempotency/request hash与本spec的top-level immutable identity；child allocation MUST由`(tenant_id,budget_owner_run_id,delegation_claim_id,usage_call_id)`唯一关联。三者都按对应版本化immutable identity区分exact replay与内部`budget.operation_conflict`；allocation还 MUST绑定同一owner与正确target sub-snapshot identity。恢复 SHALL复用原claim/allocation、request hash、identity、reservation与结果，MUST NOT重复预约或重放外部调用。任一active `reserved|needs_review` shared-budget claim MUST阻止其budget owner root terminal；terminal一旦可见，shared ledger MUST不再接受新的operation claim。

#### Scenario: 副作用前崩溃可确定释放
- **WHEN** reservation 已提交且durable `side_effect_state=not_started`，受信 fencing 证明 provider、child 与 queue 副作用均未开始
- **THEN** recovery 继续原 operation 或幂等释放原 reservation，不创建第二 claim

#### Scenario: 未结算 claim 阻止 terminal
- **WHEN** parent 准备发布 terminal 但 direct、delegation 或 child allocation 仍为 reserved/needs_review
- **THEN** terminal 写入被拒绝；13.9 reader 不得观察到提前 terminal 或 EOF

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

### Requirement: Structured reservation覆盖有限repair最坏情况
Shared parent budget SHALL 在结构化provider副作用前以checked arithmetic计算`provider_request_limit=transport_attempt_limit * (1+repair_limit)`，并预约每次冻结`trusted_input_token_bound + output_token_cap`及对应cost bound的总和。该总reservation、schema identity、transport/repair policy MUST 进入immutable operation identity；direct与delegation allocation遵守相同公式。Input/output price values与price source ref/version分别 MUST 成对完整；cost启用必须绑定完整source，cost关闭允许price values/bounds为null且完整catalog/source identity继续进入route/evidence。任一半pair、超出owner/Agent hard limit或算术溢出时 MUST 在公开structured seam以`budget.reservation_rejected`零claim/provider副作用拒绝，内部`ModelRouteError`不得逸出。

Claim建立后的确定`failed`终态只有在全部attempt副作用、usage、cost与prepared cleanup均已证明完整时才按actual结算并释放未使用reservation；核心为每个repair/transport ordinal建立fresh prepared call并显式推进循环，只有`StructuredProviderPrepareError(retryable=true)`或send前取消能由核心构造完整proof并以零actual收口。任何到达send的attempt都只对应一个provider-local request并由核心映射全局ordinal；structured收到HTTP response、call error、cancel/deadline或未知异常后都停止transport retry，classifier不适用。Send后usage不完整或cleanup失败一律使direct/allocation/owner ledger一致保持`needs_review`并保留reservation。任一request结果、usage、取消、cleanup、commit ack或request/repair基数不确定都不得借`failed`或伪造计数提前退款。

#### Scenario: Direct与allocation使用相同structured公式
- **WHEN** 相同route/schema/repair policy分别从root direct和delegated child调用
- **THEN** 两者 SHALL 以同一单次bound乘以request limit，并各自受owner/allocation上限约束，不放大额度

#### Scenario: 预算不足在发送前拒绝
- **WHEN** structured总reservation超过token或cost hard limit
- **THEN** ledger SHALL 不建立started副作用、不调用provider并返回封闭`budget.reservation_rejected`，错误不公开余额或hard limit

#### Scenario: Cost关闭仍保留完整catalog身份
- **WHEN** owner关闭cost hard limit且冻结route的input/output price values与cost bounds为null，但catalog/source ref/version完整
- **THEN** structured planning SHALL 接受该route并把完整身份带入durable route/evidence；任一value或source半pair则在claim/provider前以`budget.reservation_rejected`关闭失败

### Requirement: Structured settlement与replay不丢失repair影响
成功、invalid或repair exhausted只在所有started attempts启用维度actual完整时 SHALL 以全部attempt实际token/cost原子替换reservation。任一started/unknown attempt维度不完整时claim、allocation与owner ledger MUST 保持needs-review及不低于可信影响。相同stable operation exact replay只复用首次identity/result；schema、repair policy或语义请求变化必须在provider前冲突，不能创建第二claim。

#### Scenario: 全部actual完整后原子结算
- **WHEN** 两次structured request的token/cost均完整且最终valid或exhausted
- **THEN** shared budget SHALL 以两次actual总和一次性结算并保存exact durable result

#### Scenario: Unknown不释放reservation
- **WHEN** 第二次request已started但结果或usage未知
- **THEN** claim/ledger SHALL 进入needs-review并保留未决reservation，恢复不得退款、repair或重调provider

#### Scenario: Mark commit ack未知对direct与allocation共同围栏
- **WHEN** durable mark commit ack未知但send仍可证明未调用
- **THEN** direct claim或delegation allocation及其owner/top claim/ledger SHALL 一致进入needs-review，actual token/cost保持null并保留完整reservation；零provider request proof不得授权退款

#### Scenario: Schema冲突不创建第二claim
- **WHEN** 同一operation slot以不同schema identity或repair limit重放
- **THEN** repository SHALL 返回immutable identity conflict，现有claim/result不变且provider调用为零
