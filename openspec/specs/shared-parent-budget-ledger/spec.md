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

Direct/allocation identity MUST在最终actual route与trusted intent确定后、任何shared reservation/event-capacity mutation或provider副作用前，由可信runtime对以下封闭字段生成：`ownership_kind=direct|allocation`、`run_id`、`agent_id`、allocation时非空的`delegation_claim_id`、`usage_kind=model|embedding`、稳定语义operation slot、tenant-scoped keyed request fingerprint及key version、owner tree snapshot ID、适用agent sub-snapshot ID、provider/model、price source ref/version、embedding cache-key digest（model时为null）、cost-enabled状态与各启用维度trusted bound。

Delegation top-level identity MUST在`0015`按`(tenant,parent,idempotency_key)`与normalized request hash唯一定位或准备创建relation之后、任何`0016`claim/reservation、event-capacity、child或queue副作用之前生成。其版本化canonical payload MUST封闭包含`ownership_kind=delegation`、parent `run_id`、source/target `agent_id`、`delegation_claim_id`、`usage_kind=delegation`、`operation_slot=idempotency_key`、对`0015`同一normalized request canonical bytes生成的tenant-scoped keyed fingerprint及key version、owner tree snapshot ID、target agent sub-snapshot ID、target frozen route/price catalog digest、cost-enabled状态与本次可信top-level token/cost reservation bound。Top-level claim本身不调用provider，因此provider/model/单一price source/cache-key字段 MUST固定为null；target catalog digest MUST覆盖该target封闭允许routes及每条route的price refs/versions和必需price值，不能用null字段跳过route/price绑定。`0015` request hash继续证明请求幂等，`0016` identity另外证明budget replay context；两者都必须一致才是exact replay。

三类request fingerprint MUST由versioned tenant-scoped key对各自canonical semantic request bytes生成；数据库只保存opaque fingerprint与key version，不保存key、child input、prompt或embedding原文。Identity canonical JSON MUST使用UTF-8、排序键、紧凑分隔符并拒绝NaN/Infinity，随后以固定hash算法生成持久化hash。动态current balance、event capacity、approval result、cache hit/miss结果、provider result、latency和错误不得进入identity。Cache lookup的稳定cache-key digest进入usage identity，但hit/miss由首次原子提交的durable result决定：提交前失败可重做只读lookup，提交后同identity必须重放首次结果。相同stable key只有对应`identity_schema_version`与identity hash逐值相同才是exact replay；任一封闭字段、fingerprint或版本不同 MUST在owner/relation mutation、delegation子额度、parent budget、capacity检查及外部副作用前返回内部`budget.operation_conflict`。Direct seam保持该内部code；allocation冲突若向parent delegation结果传播 MUST封闭映射为既有`delegation.execution_failed`。Delegation top-level replay MUST先验证`0015` normalized request hash，再验证`0016` identity；request hash异值或同hash但identity异值都公开映射既有`delegation.idempotency_conflict`，内部MAY记录不含fingerprint、snapshot内容、route/price或动态数值的`budget.operation_conflict` evidence。Insert/unique race MUST回滚后重读并应用相同判定。

#### Scenario: 相同 usage_call_id 的不同请求发生 identity conflict
- **WHEN** 同一tenant/owner/`usage_call_id`以不同request fingerprint、usage kind、actual route、snapshot/sub-snapshot、price version或trusted bound重试
- **THEN** direct在读取current balance或event capacity前返回`budget.operation_conflict`，不重放旧result、不新增claim且provider调用为零；错误不得公开fingerprint或identity字段

#### Scenario: Child allocation 同 key 异 identity 在子额度前冲突
- **WHEN** 同一tenant/owner/delegation claim/`usage_call_id`以不同child request fingerprint、usage kind、actual route、target sub-snapshot、price version或trusted bound重试
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
