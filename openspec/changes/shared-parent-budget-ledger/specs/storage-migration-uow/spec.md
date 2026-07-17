## ADDED Requirements

### Requirement: 0016 前滚共享 parent budget ledger
Alembic revision `0016` SHALL 直接依赖 `0015`，新增 tenant-scoped parent ledger、top-level operation claim 与 delegation child allocation（或经合同证明等价的受约束记录），并以唯一关联连接既有 `usage_call_id`、delegation claim/reservation。Ledger MUST 以非空 `(tenant_id,budget_owner_run_id)` 为唯一键并 tenant-fenced 引用 execution-tree root `AgentRun.run_id`；claim/allocation MUST 持有相同 owner FK，不得把 nullable `AgentRun.parent_run_id` 直接当作 owner。`0014` usage outbox/event capacity 与 `0015` relation/reservation/aggregate 的历史字段和职责 MUST 保持不变。所有 ledger mutation MUST 在 owner root/ledger row lock 或等价 CAS 的同一 UoW 中校验 token/cost 不变量，SQLite 与 PostgreSQL 结果一致。

全新 direct model/embedding operation SHALL 在同一 application UoW 中创建或重放 `0016` direct claim、`0014` usage settlement/outbox 与 event-capacity reservation；全新 delegation SHALL 在同一 UoW 中创建或重放 `0016` top-level claim、`0015` delegation relation/reservation 与 `0014` ordered evidence/event-capacity reservation。任一 owner、budget、capacity、relation、唯一键或 replay-integrity 检查失败 MUST 回滚整组，禁止留下只有 shared claim 或只有 `0014`/`0015` operation 的半提交状态。可信 provider/child result 的 durable persistence、direct/allocation settlement、delegation top-level delta 与 parent aggregate update MUST 同一 UoW 提交；event publish 位于提交后并复用既有 outbox recovery。

#### Scenario: Direct claim 与 0014 operation 原子提交
- **WHEN** direct model/embedding 在 shared reservation、usage outbox 或 event-capacity 任一步发生预算不足、容量不足、唯一键冲突或事务失败
- **THEN** `0016` direct claim、`0014` usage settlement/outbox 与 event-capacity reservation 全部提交或全部回滚；恢复不得观察到单边记录，也不得重复 provider

#### Scenario: Delegation claim 与 0014/0015 operation 原子提交
- **WHEN** delegation 在 shared reservation、`0015` relation/reservation、`0014` ordered evidence 或 event-capacity 任一步失败
- **THEN** `0016` top-level claim、`0015` relation/reservation、`0014` operation group 与 capacity reservation 全部提交或全部回滚，并严格使用shared-parent-budget-ledger规定的检查/错误优先级；capacity-only保留`event.sequence_exhausted`，不得笼统改写为`delegation.*`，且不发布lifecycle event、不创建child或queue

#### Scenario: Result persistence 与 shared settlement 原子提交
- **WHEN** provider/child 已返回可信结果，进程在 result persistence、allocation/top-level delta 或 parent aggregate update 任一步失败
- **THEN** 可信 result、`side_effect_state=result_committed`与全部 shared-budget settlement mutation 在同一 UoW 全部提交或回滚；新writer不能留下result-only状态，提交后的event publish失败只由既有outbox补投，不得重放provider、child或queue

#### Scenario: 0016 upgrade 安全 backfill
- **WHEN** writers/workers 已停止且既有 `0014`/`0015` evidence 可唯一解释
- **THEN** migration 在任何 mutation 前整批预检，并按以下固定矩阵 backfill；任一关系或数值不能唯一证明时在 DDL/UPDATE 前整批 fail closed

#### Scenario: Backfill 区分 root direct 与 delegated child
- **WHEN** migration 扫描既有 `run_evidence_outbox` usage，并能由 `agent_runs.parent_run_id` 与唯一 `agent_delegations.child_run_id` relation 逐值证明归属
- **THEN** `parent_run_id IS NULL` 的 root 把自身 `run_id` 规范化为非空 `budget_owner_run_id`；child 必须由同 tenant 的 `parent_run_id` 与唯一 delegation relation 共同解析到该 root owner。只有 root 自身 usage 建立顶层 direct claim；child usage 从 direct scan 排除，并按 `(tenant,budget_owner_run_id,delegation,child_run_id,usage_call_id)` 建立唯一 allocation linkage。跨 tenant、嵌套 child、parent-child 字段不一致、child 缺失或命中多个 relation 时整批 fail closed

#### Scenario: Backfill 隔离多个 root 并合并同 root claims
- **WHEN** 同一 tenant 存在两个 `parent_run_id=null` 的 root runs，且其中一个 root 同时有 direct usage 与唯一 child/delegation evidence
- **THEN** migration 为两个 root 分别建立以各自 `run_id` 为 owner 的 ledger；同一 root 的 direct claim、delegation claim与child allocation使用同一owner，另一个root的余额和状态完全隔离

### Requirement: 0016 只为可继续执行的 legacy tree 回填可证明 snapshot
`0016` SHALL 在 DDL/UPDATE 前把每个 legacy root tree 整批分类为 `legacy_closed` 或 `snapshot_backfill_required`。`legacy_closed` MUST 同时满足：root 已 terminal 且具备dialect等价的durable terminal closure proof；全部 `0014` usage/ordered outbox 已处于 `published|cancelled`；event capacity 无 outstanding reservation；全部 `0015` delegation 已 `settled|released` 且无 needs-review、pending child、queue、approval 或 recovery 工作。PostgreSQL closure proof SHALL 是与root status逐值一致的唯一terminal canonical event；SQLite closure proof SHALL 是terminal run status、`run_event_capacity.terminal_reservation=0`与`outstanding_reserved_event_count=0`的组合，依赖既有local JSONL/capacity原子写合同，Alembic不得猜测或扫描未受约束路径。该类 tree SHALL 原样保留 `0014`/`0015` 历史，不建立 `0016` ledger/claim/allocation，也不得在升级后恢复任何新 operation。

其余仍需继续执行或恢复的 `snapshot_backfill_required` tree，其 root ledger snapshot SHALL包含hard token/cost limits、descriptor version、budget/config version、frozen route policy允许的provider/model refs，以及每个cost-enabled route的price source ref/version；cost-disabled状态 MUST显式持久化。Migration MUST只接受root run、checkpoint或durable evidence在创建/执行时已持久化引用的不可变版本标识，并用该标识解析内容可校验、hash/version一致的versioned descriptor/config history与price catalog记录。Migration-time current resolver、当前reload后配置、当前price、`0015` reservation数值、usage actual、默认值或零值 MUST NOT作为历史snapshot来源。Child MUST逐值继承其root snapshot。

在任何DDL/UPDATE前，migration MUST整批验证分类条件；每个需回填root的引用必须存在且唯一、源记录可用、tenant/agent/descriptor/config/hash/version一致、hard limits与允许route完整，并在cost启用时验证全部允许route的price refs/versions可解析。既不满足`legacy_closed`又缺少完整snapshot、只能取得当前配置、多来源冲突、内容hash/version不符、cost-enabled price缺失或child evidence暗示不同snapshot时 MUST整批fail closed。维护流程 SHALL 先用旧 writer drain/reconcile 使无历史snapshot的合法旧tree成为`legacy_closed`；不得以migration猜值替代该步骤。SQLite与PostgreSQL MUST逐值产生相同分类、snapshot或拒绝。

#### Scenario: 无历史 snapshot 的封闭 legacy tree 可安全升级
- **WHEN** 合法 `0015` 数据库中的旧 root 没有 immutable snapshot 引用，但 root 具备上述dialect等价terminal closure proof，全部 usage/delegation/event-capacity/queue/approval/recovery 状态都满足 `legacy_closed`
- **THEN** `0016` 保留其 `0014`/`0015` 历史且不建立 shared ledger/claim/allocation；升级成功，reload 或新 writer 不得让该 tree 恢复新 operation

#### Scenario: 无历史 snapshot 的在途 tree 必须先 drain
- **WHEN** 旧 root 缺少 immutable snapshot，且仍非 terminal、存在 pending/needs-review/outstanding evidence，或不能逐值证明 `legacy_closed`
- **THEN** SQLite 与 PostgreSQL 都在 DDL/UPDATE 前整批拒绝，并要求旧 writer 先完成 drain/reconcile；不得用 current config、reservation 或 actual 合成 snapshot

#### Scenario: 可证明历史 snapshot 逐值回填
- **WHEN** root的durable run/checkpoint/evidence引用唯一immutable descriptor/config version及其允许route/price versions，且child relation与root引用一致
- **THEN** `0016`逐值回填hard limits、版本和route/price refs，child复用同一owner snapshot；迁移后reload不改变该root恢复、fallback或approval resume使用的snapshot

#### Scenario: 当前配置不能替代缺失历史版本
- **WHEN** `snapshot_backfill_required` root缺少immutable version引用、引用源已不存在/校验失败，或只能由migration-time current resolver取得配置和price
- **THEN** SQLite与PostgreSQL都在任何DDL/UPDATE前整批拒绝，不采用当前值、reservation、actual、默认值或零值；只有严格满足`legacy_closed`的tree可以不建立snapshot而保留旧历史

#### Scenario: Root 与 child snapshot 证据冲突
- **WHEN** child的durable identity/version evidence与其唯一budget owner root的descriptor/config/route/price snapshot任一逐值不一致
- **THEN** migration在写入ledger/claim/allocation前整批拒绝，不为child建立独立snapshot或把冲突静默归一

#### Scenario: Backfill root direct usage
- **WHEN** root direct usage 有可信 settled actual，或有确定 `provider_called=false` 的零预算结果
- **THEN** migration只在上述完整snapshot预检通过后建立settled direct claim/actual impact；enabled维度actual超frozen parent limit时保留actual、标记claim/parent needs_review并封锁新operation与terminal。若外部副作用可能发生但enabled维度actual不可得，且历史没有可信reservation bound，则整批fail closed，不猜测0或历史均值。Cost-disabled且组合合法的`cost=null/unavailable`不产生cost impact或needs_review；非法cost/status组合仍整批拒绝

#### Scenario: Backfill delegated child allocation 与 settled delegation
- **WHEN** `0015 settled` delegation 的全部 child `usage_call_id` 都有可信 settled evidence，relation-first child 集合与可信 aggregate 逐值一致
- **THEN** 每个child按root snapshot已启用维度建立`state=settled`、`backfill_source=legacy_settled`、`reservation_bound=null`、impact=trusted actual的allocation；child cache hit以`provider_called=false`证明settled/zero-impact allocation并在budget aggregate中作为已知0，不建立direct claim、不解释为unknown。Cost-disabled时合法unavailable不建立cost impact。这些usage都不建立direct claim。若child actual/aggregate在已启用维度合计不超过原delegation reservation，顶层claim为settled/actual impact；若合计超过原reservation，顶层impact取`max(original,child actual sum,trusted aggregate)`并标记claim/parent needs_review、封锁新operation与terminal。Aggregate缺失、与child合计不一致或任一维度evidence非法时整批fail closed

#### Scenario: Backfill active 与 needs-review delegation
- **WHEN** `0015 reserved|needs_review` delegation 已有关联 child usage
- **THEN** 可信 settled child 建立 legacy settled allocation；已开始但结果未知且无历史 operation bound 的 child 建立 `state=needs_review`、`reservation_bound=null` 的唯一 linkage，不虚构 allocation 数值。顶层 claim 保留至少原 delegation reservation，并取其与全部可信 child actual 合计/可信 aggregate的最大值；存在未知 allocation、原状态为 needs_review 或 known actual 超 reservation 时 parent needs_review并封锁新 operation与terminal

#### Scenario: Backfill released delegation
- **WHEN** `0015 released` delegation 可证明 child、queue、provider 与业务执行副作用均未发生，并且 lifecycle evidence 精确为合法 pre-child `delegation.claimed -> delegation.failed` 稳定事件对，或为可由 `0014` 确定性恢复成该事件对的稳定 pending outbox
- **THEN** migration 允许并保留该 claimed/failed 内部 evidence，先完成或复用其确定性 recovery，再建立 released 顶层 detail linkage、impact=0，使同 key恢复不重新预约；存在 child relation、`delegation.child.created|completed`、usage、queue/provider/业务执行副作用，或 lifecycle event缺失、额外、乱序、payload不稳定时整批fail closed

#### Scenario: Released migration 正反例在两种数据库一致
- **WHEN** SQLite或PostgreSQL分别包含合法 `released + claimed/failed`，以及含 child.created、child relation、usage或queue/provider evidence的非法 released fixture
- **THEN** 两种数据库都只允许合法fixture升级并保留内部event，非法fixture在任何DDL/UPDATE前整批拒绝

### Requirement: 0016 downgrade 不删除共享预算事实
`0016 -> 0015` downgrade SHALL 只在 shared ledger/claim/allocation evidence 全空且 Alembic x 参数精确为 `allow_empty_evidence_downgrade=true` 时允许。参数缺失、重复、非法或存在任一历史/active evidence 时 MUST 在 DDL 前拒绝，不得删除、释放、改写预算事实或破坏 `0014`/`0015` 兼容读取。

#### Scenario: 有 evidence 时 downgrade 被阻止
- **WHEN** 任一 parent ledger、direct/delegation claim 或 child allocation 存在，即使操作者提供 opt-in
- **THEN** SQLite 与 PostgreSQL 都在 DROP/UPDATE 前稳定拒绝，全部 evidence 和保守占用保持不变
