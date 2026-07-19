# storage-migration-uow Specification

## Purpose
定义 SQLite/PostgreSQL 共用的迁移、repository 与 Unit of Work 契约，使 P0 核心实体具备一致的租户隔离、事务边界、回滚语义和可验证 schema。
## Requirements
### Requirement: Async storage migration 创建 P0 核心 schema
`agent_harness` package SHALL 提供 SQLAlchemy 2.0 async typed models 和 Alembic migration，用于创建 P0 所需的租户、session、run、checkpoint、event、trace、artifact、eval、policy 和 audit 核心表。

#### Scenario: Local SQLite migration 创建 schema
- **WHEN** developer 使用 local profile 执行 migration
- **THEN** SQLite database 包含 `tenants`、`sessions`、`agent_runs`、`checkpoints`、`canonical_events`、`trace_refs`、`artifacts`、`eval_cases`、`eval_runs`、`policy_rules` 和 `audit_logs` 表，并记录当前 migration revision

#### Scenario: Service PostgreSQL migration 创建 schema
- **WHEN** developer 使用 service profile 连接 PostgreSQL 执行 migration
- **THEN** PostgreSQL database 包含同一批核心表，并记录当前 migration revision

### Requirement: Repository 和 UnitOfWork 隔离 ORM session
package SHALL 暴露 repository interface 和 UnitOfWork，使 app、API、agent、eval 和 runtime 调用方不直接依赖 SQLAlchemy session。

#### Scenario: Repository contract 在 adapters 间一致
- **WHEN** 同一 repository contract tests 分别运行在 SQLite 和 PostgreSQL adapter 上
- **THEN** tenant、session、run 和 checkpoint 的创建、查询、更新和事务回滚行为一致

#### Scenario: 业务入口不直接持有 ORM session
- **WHEN** static import/session boundary check 扫描 `templates/service-app/app/*`、`templates/service-app/agents/*`、`examples/*` 和 eval 入口
- **THEN** 扫描不到直接创建或传递 SQLAlchemy `Session` / `AsyncSession` 的业务代码

### Requirement: Doctor 报告 storage、migration 和 service dependency 状态
`agent-harness doctor` SHALL 对选定 profile 报告 storage kind、database connectivity、migration revision、Redis connectivity 和 eval directory 状态。

#### Scenario: Local doctor 不需要外部服务
- **WHEN** developer 运行 `agent-harness doctor --profile local`
- **THEN** command 报告 SQLite/local evidence path 状态，并在无 PostgreSQL/Redis 时成功退出

#### Scenario: Service doctor 报告 PostgreSQL 和 Redis 状态
- **WHEN** developer 运行 `agent-harness doctor --profile service`
- **THEN** command 尝试连接 PostgreSQL 和 Redis，报告 migration revision 和 Redis reachability；连接失败时使用结构化诊断 non-zero 退出

### Requirement: Embedding cache tenant scope 迁移保留 evidence 并阻止不安全降级
Alembic revision `0012a_embedding_cache_tenant_scope` SHALL 从 `0012_service_runtime_execution_context` 升级，把旧 `embedding_cache` 物理表切换为 `tenant_embedding_cache`，并把唯一约束收紧为 `(tenant_id, provider, model, input_hash)`；目标约束/索引名固定为 `uq_tenant_embedding_cache_tenant_provider_model_hash`、`ix_tenant_embedding_cache_tenant_id`、`ix_tenant_embedding_cache_input_hash`。升级后数据库中 MUST 不存在可供旧 binary 查询的 `embedding_cache` table/view/alias。SQLite 与 PostgreSQL MUST 保留既有 row 的 id、tenant、provider、model、input hash、`vector_ref`、原 metadata 键与时间戳。Migration MUST 在任何 DDL/UPDATE 前完整预检 legacy metadata，然后确定性增量写入 `cache_status`、`vector_ref`、`provider_latency_status` 与 nullable `provider_latency_ms`。统一键缺失时从合法 legacy 键/row 列派生，已存在时必须类型合法且与派生值相等；存在合法 `provider_latency_ms` 或 legacy `latency_ms` 时，status MUST 为 `recorded` 且两键并存时数值相等。两种 latency key 都缺失是旧合同允许的状态，migration MUST 保留全部原 metadata 键并补 `provider_latency_status=unavailable`、`provider_latency_ms=null`，MUST NOT 猜测为 `0`。统一键与 legacy 键冲突、值非法、`vector_ref` 与 row 列不等，或 latency status/value 组合不一致时 MUST 整批 fail closed，不覆盖、猜值或部分提交。`cache_status` 与 legacy `cache` 都缺失时按旧 row 只由 provider miss 创建的合同派生 `miss`。后续 trace revision `0013` MUST 以 `0012a_embedding_cache_tenant_scope` 为直接前置。只要存在任一 tenant cache evidence，downgrade MUST fail closed，且不得删除、改写或暴露该 evidence；旧 binary 因旧物理表不存在而在读取前失败，新 binary 在未升级的 `0012` schema 上因新物理表不存在而失败。即使新表为空，downgrade 也只有在操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true` 时才能恢复名为 `embedding_cache` 的 `0012` 旧表、旧三列约束和旧索引名；参数缺失、重复、值不是精确小写 `true` 或存在任一 evidence 都必须在 DDL 前拒绝。

#### Scenario: SQLite 和 PostgreSQL 升级保留既有 cache evidence
- **WHEN** 操作者把含既有 embedding cache row 的 SQLite 或 PostgreSQL 从 `0012` 升级到 `0012a`
- **THEN** 所有既有 row 字段和原 metadata 键逐值保持不变，统一 metadata 字段被补齐，`tenant_embedding_cache` 四列唯一约束生效，旧物理表名不存在，不同 tenant 可保存相同 provider/model/input hash，同 tenant 重复 identity 被拒绝

#### Scenario: 非法 legacy metadata 在 mutation 前阻止升级
- **WHEN** 任一 legacy row 的 metadata 不是 object、统一键类型非法、统一键与 legacy/row 派生值冲突，provider latency 值是 bool/负数/非有限值，或 latency status/value 组合不一致
- **THEN** SQLite/PostgreSQL migration 在任何 constraint、row 或 revision mutation 前整批失败，错误不回显 metadata 内容，全部 evidence 保持原样

#### Scenario: 历史 latency 缺失可无损升级
- **WHEN** `0012` 合法 cache row 的 metadata 同时缺少 `provider_latency_ms` 与 legacy `latency_ms`
- **THEN** migration 保留该 row、时间戳与全部原 metadata 键，补 `provider_latency_status=unavailable` 和 `provider_latency_ms=null` 后继续原子升级；不得猜测 `0` 或因旧合同允许的缺失阻断整库

#### Scenario: 新 cache 写入必须记录真实 latency 状态
- **WHEN** `0012a` repository/provider 尝试写入新的 cache miss，但缺少 `provider_latency_status=recorded` 或 `provider_latency_ms` 不是非 bool 非负 number
- **THEN** repository 在持久化前拒绝且不创建 cache row；历史 migration 专用的 unavailable 状态不能被新写入复用

#### Scenario: 缺失或相等的统一 metadata 可安全补齐
- **WHEN** 统一键缺失但 legacy/row 派生值合法，或统一键已经存在且与派生值逐值相等
- **THEN** migration 只补缺失键，保留全部原键和值；已有相等键不改写

#### Scenario: 显式确认的空数据库允许降级
- **WHEN** `tenant_embedding_cache` 为空且操作者以 `-x allow_empty_evidence_downgrade=true` 执行 `0012a -> 0012` downgrade
- **THEN** migration 恢复名为 `embedding_cache` 的旧表与三列约束并记录 `0012_service_runtime_execution_context` revision，不删除任何业务 evidence

#### Scenario: 空数据库但没有显式确认仍拒绝
- **WHEN** `tenant_embedding_cache` 为空，但 Alembic x 参数缺失、重复或不是精确 `allow_empty_evidence_downgrade=true`
- **THEN** migration 在任何 constraint drop 前以脱敏错误拒绝，revision 和 schema 保持 `0012a`

#### Scenario: 存在 cache evidence 时降级 fail closed
- **WHEN** SQLite 或 PostgreSQL 的 `tenant_embedding_cache` 存在任一 row，操作者尝试 `0012a -> 0012` downgrade
- **THEN** migration 在任何 constraint drop 或 row mutation 前以脱敏错误拒绝，revision 与全部 cache evidence 保持不变

#### Scenario: 后续 trace migration 保持单一线性 head
- **WHEN** 数据库从当前 head 继续升级 Phase 13.6A trace revision `0013`
- **THEN** Alembic revision 链严格为 `0012_service_runtime_execution_context -> 0012a_embedding_cache_tenant_scope -> 0013_run_trace_correlation -> 0013a_run_trace_event_hardening`，不存在并行 head、改写已应用 revision 或跳过 tenant 修复的路径

#### Scenario: 新旧 application/schema 组合双向 fail closed
- **WHEN** 旧 binary 在 `0012a` schema 查询 `embedding_cache`，或新 binary/repository 在未升级的 `0012` schema 查询 `tenant_embedding_cache`
- **THEN** 数据库在返回任一 cache row 前以缺失关系失败，不存在兼容 table/view/alias；contract 必须分别断言零跨租户结果和零 cache mutation

### Requirement: Trace migration downgrade 不删除 canonical trace evidence
Alembic revision `0013` SHALL 以 `0012a_embedding_cache_tenant_scope` 为直接前置，并只在 SQLite/PostgreSQL 数据库不存在任何 `run_trace_bindings`、run-scoped canonical trace 或 backfill 完成 evidence，且操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true` 时允许 downgrade 到 `0012a` trace-nullable schema。参数缺失、重复、值不是精确小写 `true` 或存在任一历史/活跃 evidence 时，downgrade MUST 在 DDL 前 fail closed、保留兼容读取且不得删除或置空 trace evidence；SQLite 与 PostgreSQL MUST 遵守相同结果，且 `0013` 不得绕过 `0012a` 自身的 cache evidence downgrade 门禁。

#### Scenario: 空且可丢弃数据库允许回退
- **WHEN** 操作者对不存在 binding、run-scoped canonical trace 或 backfill evidence 的数据库以 `-x allow_empty_evidence_downgrade=true` 执行 `0013 -> 0012a` downgrade
- **THEN** migration 恢复 `0012a` trace-nullable schema，并由 SQLite/PostgreSQL contract 验证没有删除业务 evidence

#### Scenario: 任一 trace evidence 阻断破坏性回退
- **WHEN** 数据库存在 root binding、run/child trace projection、run-scoped event/approval/audit/tool/eval trace 或 backfill 完成 evidence
- **THEN** downgrade 在任何 DROP/UPDATE 前以脱敏错误 fail closed，保留 `0013` 兼容读取且不删除、不置空 evidence

#### Scenario: 未确认的空数据库拒绝回退
- **WHEN** 数据库没有 trace evidence，但 x 参数缺失、重复或不是精确 `allow_empty_evidence_downgrade=true`
- **THEN** downgrade 在任何 DROP/UPDATE 前拒绝，revision 和 `0013` schema 保持不变

### Requirement: 已发布 0013 shape 漂移必须线性前滚
系统 MUST 保留已发布 `0013_run_trace_correlation` 的 revision 身份，并以唯一线性后继 `0013a_run_trace_event_hardening` 收敛现场旧 `0013` 与当前最终 `0013` 的事件 schema。普通运行入口 MUST 只接受 `0013a` head，不得仅因数据库 stamp 为 `0013` 就跳过物理 shape 修复。`0013a` MUST 在任何 DDL/DML 前精确区分两种允许来源：旧完整 shape 具有 `record_scope` 但缺 `stream_id`、tenant/stream 唯一键、scope CHECK、三列 run-owner FK、audit CHECK 与 agent run 三列引用键；最终完整 shape 已具备全部目标列和约束。旧 shape 必须先完整预检 scope、legacy stream、run/tenant/trace ownership、序列唯一性和 audit scope，再确定性把旧 `run_id` 复制为 `stream_id`、把 non-run 数据库 ownership 置空并补齐目标约束；最终 shape 只验证并 no-op。混合、部分或不兼容 shape MUST 在 mutation 前 fail closed。`0013a -> 0013` downgrade 只回退 revision stamp并保留硬化 schema和 evidence；真正的 `0013 -> 0012a` 破坏性回退继续由既有精确 opt-in 与空 evidence 门禁负责。后续 `0014` MUST 以 `0013a_run_trace_event_hardening` 为直接前置。

#### Scenario: 旧同名 revision 不再假通过
- **WHEN** 数据库 stamp 为 `0013_run_trace_correlation`，但仍是缺少 `stream_id` 与最终事件约束的旧完整 shape
- **THEN** 普通入口在创建 run、event 或其他业务副作用前报告 migration required；显式前滚到 `0013a` 后保留 legacy stream、正确分类 run/non-run ownership并开放写入

#### Scenario: Fresh 与旧库收敛到同一唯一 head
- **WHEN** fresh 数据库从 `0012a` 执行 `0013 -> 0013a`，或旧 service 数据库从已 stamp 的旧 `0013` 执行 `0013a`
- **THEN** 两条路径都得到相同最终列、唯一键、scope CHECK、三列 run-owner FK、audit CHECK 与唯一 Alembic head；partial shape 不产生任何 DDL/DML

### Requirement: Evidence outbox migration downgrade 不删除结算事实
Alembic revision `0014` SHALL 以 `0013a_run_trace_event_hardening` 为直接前置，并只在 SQLite/PostgreSQL 数据库不存在任何 usage settlement、approval resolution、terminal、event capacity reservation 或其他 `run_evidence_outbox` evidence，且操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true` 时允许 downgrade 到 `0013a_run_trace_event_hardening`。参数缺失、重复、值不是精确小写 `true` 或存在任一历史/活跃 outbox、settlement、capacity evidence 时，downgrade MUST 在 DDL 前 fail closed、保留兼容读取且不得删除、重排或伪造 evidence；SQLite 与 PostgreSQL MUST 遵守相同结果。

Upgrade MUST 在 API/worker writers 已停的窗口，先完整预检既有 run/event/checkpoint/approval/tool durable state。已有 terminal 的 run 不建立预约；每个非 terminal run MUST 建立一个 terminal reservation，并把该 run 已持久化的最大 `seq`（无 event 时为 `0`）回填为可信 high-water mark，不能使用 event row count。只有能从持久化状态映射到封闭、版本化 `operation_kind` registry 的活跃 operation 才能按对应最大 prerequisite event 数回填 outstanding reservation；未知 operation kind、矛盾状态、已有 seq 越界、high-water mark 与最大已持久化 `seq` 不一致，或 `highest_persisted_seq + outstanding + terminal` 超限 MUST 在任何 DDL/UPDATE 前整批 fail closed。完成后 repository MUST 以数据库约束或同事务 CAS 维护 high-water/outstanding/terminal 容量不变量，并在同一 run 锁/事务内消费预约、插入 event 和推进 high-water mark。

#### Scenario: 空且可丢弃数据库允许回退
- **WHEN** 操作者对不存在任何 outbox/settlement/capacity evidence 的数据库以 `-x allow_empty_evidence_downgrade=true` 执行 `0014 -> 0013a_run_trace_event_hardening` downgrade
- **THEN** migration 移除空的 `0014` schema，并由 SQLite/PostgreSQL contract 验证没有删除业务 evidence

#### Scenario: 任一结算 evidence 阻断破坏性回退
- **WHEN** 数据库存在 started/result/published usage settlement，或 pending/published approval resolution/terminal outbox item
- **THEN** downgrade 在任何 DROP/UPDATE 前以脱敏错误 fail closed，保留 `0014` 兼容读取、event id与顺序，不重放 provider/tool且不删除 evidence

#### Scenario: 未确认的空数据库拒绝回退
- **WHEN** 数据库没有 `0014` evidence，但 x 参数缺失、重复或不是精确 `allow_empty_evidence_downgrade=true`
- **THEN** downgrade 在任何 DROP/UPDATE 前拒绝，revision、outbox 与 capacity schema 保持不变

#### Scenario: Upgrade 为旧 run 回填可信容量
- **WHEN** writers 已停，旧数据库同时包含 terminal run、无活跃 operation 的非 terminal run，以及能由持久化状态映射到封闭 operation kind 的 waiting/recovery run
- **THEN** migration 对 terminal run 不建预约，对其他 run 建 terminal reservation，并只按 registry 为已知活跃 operation 回填 outstanding reservation；SQLite/PostgreSQL 逐值一致

#### Scenario: 未知活跃状态阻止容量迁移
- **WHEN** 任一非 terminal run 的活跃状态无法映射到封闭 operation kind，high-water mark 与最大已持久化 `seq` 不一致，或 highest-seq/outstanding/terminal 总和将越界
- **THEN** migration 在任何 DDL/UPDATE/revision mutation 前整批失败，旧 run、event 与状态保持不变

#### Scenario: 稀疏高序号按最大值回填容量
- **WHEN** 旧 non-terminal run 只有 `seq=1` 与 `seq=2147483646` 两条 event，且没有活跃 operation
- **THEN** migration 以 `2147483646` 回填 high-water mark并只保留 terminal reservation；任何新 operation reservation 在副作用前以 `event.sequence_exhausted` 拒绝，不能按 row count 误判容量

### Requirement: Delegation migration downgrade 不删除执行与预算 evidence
Alembic revision `0015` SHALL 只在 SQLite/PostgreSQL 数据库不存在任何 delegation claim、child relation、budget reservation 或 aggregation evidence，且操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true` 时允许 downgrade 到 `0014`。参数缺失、重复、值不是精确小写 `true` 或存在任一历史/活跃 delegation/reservation/aggregation evidence 时，downgrade MUST 在 DDL 前 fail closed、保留兼容读取且不得删除、释放或改写 evidence；SQLite 与 PostgreSQL MUST 遵守相同结果。

#### Scenario: 空且可丢弃数据库允许回退
- **WHEN** 操作者对不存在 delegation、reservation 或 aggregation evidence 的数据库以 `-x allow_empty_evidence_downgrade=true` 执行 `0015 -> 0014` downgrade
- **THEN** migration 移除空的 `0015` schema，并由 SQLite/PostgreSQL contract 验证没有删除业务 evidence

#### Scenario: 任一 delegation evidence 阻断破坏性回退
- **WHEN** 数据库存在 idempotency claim、parent/child relation、`reserved|settled|released|needs_review` reservation 或 aggregation record
- **THEN** downgrade 在任何 DROP/UPDATE 前以脱敏错误 fail closed，保留 `0015` 兼容读取，不归还未知预算且不删除 evidence

#### Scenario: 未确认的空数据库拒绝回退
- **WHEN** 数据库没有 `0015` evidence，但 x 参数缺失、重复或不是精确 `allow_empty_evidence_downgrade=true`
- **THEN** downgrade 在任何 DROP/UPDATE 前拒绝，revision 和 delegation schema 保持不变

### Requirement: 0016 前滚共享 parent budget ledger
Alembic revision `0016` SHALL 直接依赖 `0015`，新增 tenant-scoped parent ledger、top-level operation claim 与 delegation child allocation（或经合同证明等价的受约束记录），并以唯一关联连接既有 `usage_call_id`、delegation claim/reservation。Ledger MUST 以非空 `(tenant_id,budget_owner_run_id)` 为唯一键并 tenant-fenced 引用 execution-tree root `AgentRun.run_id`；claim/allocation MUST 持有相同 owner FK，不得把 nullable `AgentRun.parent_run_id` 直接当作 owner。Ledger snapshot MUST保存owner envelope与root/允许target各自的agent sub-snapshot；direct、delegation top-level claim与allocation MUST保存对应版本化immutable operation identity hash、schema/key version、opaque fingerprint与非敏感关联refs。Delegation top-level row还 MUST保存既有`0015` request hash、target sub-snapshot与route/price catalog digest、可信reservation bounds，并以`(tenant_id,budget_owner_run_id,delegation_id)`唯一；delegation child allocation 的物理 stable key 与数据库 UNIQUE MUST固定为`(tenant_id,budget_owner_run_id,delegation_id,usage_call_id)`。这里两类物理列`delegation_id` MUST逐值引用唯一`AgentDelegation.id`，且与shared-budget spec中的`delegation_claim_id`完全等价，claim/allocation identity也 MUST绑定该同一值，不得另建映射或第二标识；`child_run_id`只用于relation与拓扑完整性校验，绝不进入allocation stable key或UNIQUE。数据库constraint MUST拒绝任何三类operation identity为null或ownership/schema形状不匹配。`0014` usage outbox/event capacity 与 `0015` relation/reservation/aggregate 的历史字段和职责 MUST 保持不变。所有 ledger mutation MUST 在 owner root/ledger row lock 或等价 CAS 的同一 UoW 中校验 token/cost 不变量，SQLite 与 PostgreSQL 结果一致。

`0016` MUST 在任何 DDL/UPDATE 前读取全部 `agent_runs` 与相关 `agent_delegations` 并验证全库 parent graph，而不是只从 roots 遍历 direct children。每个非root run MUST 直接指向一个存在且同tenant的root，且 MUST 有且仅有一条 tenant/source/parent/child 一致的 delegation relation；P0 的嵌套 child、孤儿、循环、跨tenant parent、缺失或重复 relation及未被任何root分类覆盖的row MUST整批fail closed。

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
- **THEN** `parent_run_id IS NULL` 的 root 把自身 `run_id` 规范化为非空 `budget_owner_run_id`；child 必须由同 tenant 的 `parent_run_id` 与唯一 delegation relation 共同解析到该 root owner。只有 root 自身 usage 建立顶层 direct claim；child usage 从 direct scan 排除，并按物理`(tenant_id,budget_owner_run_id,delegation_id,usage_call_id)`建立唯一 allocation。`child_run_id`只参与 relation 与拓扑完整性校验，不进入 stable key 或数据库 UNIQUE。跨 tenant、嵌套 child、parent-child 字段不一致、child 缺失或命中多个 relation 时整批 fail closed

#### Scenario: Backfill 隔离多个 root 并合并同 root claims
- **WHEN** 同一 tenant 存在两个 `parent_run_id=null` 的 root runs，且其中一个 root 同时有 direct usage 与唯一 child/delegation evidence
- **THEN** migration 为两个 root 分别建立以各自 `run_id` 为 owner 的 ledger；同一 root 的 direct claim、delegation claim与child allocation使用同一owner，另一个root的余额和状态完全隔离

#### Scenario: 全库拓扑反例在 DDL 前拒绝
- **WHEN** legacy 数据包含三层 parent-child、孤儿 parent、parent cycle、跨tenant parent，或 child 缺失/命中多条 delegation relation中的任一反例，即使另有合法 root 可被旧的 root/direct-child 查询选中
- **THEN** SQLite与PostgreSQL都在创建 `0016` 表或更新任何 legacy row 前整批拒绝；不得遗漏坏节点、部分升级合法 tree 或把 nested child 当成新的 root

### Requirement: 0016 只为可继续执行的 legacy tree 回填可证明 snapshot
`0016` SHALL 在 DDL/UPDATE 前把每个 legacy root tree 整批分类为 `legacy_closed` 或 `snapshot_backfill_required`。`legacy_closed` MUST 同时满足：root 已 terminal 且具备dialect等价的durable terminal closure proof；全部 `0014` usage/ordered outbox 已处于 `published|cancelled`；event capacity 无 outstanding reservation；全部 `0015` delegation 已 `settled|released` 且无 needs-review、pending child、queue、approval 或 recovery 工作。PostgreSQL closure proof SHALL 是与root status逐值一致的唯一terminal canonical event；SQLite closure proof SHALL 是terminal run status、`run_event_capacity.terminal_reservation=0`与`outstanding_reserved_event_count=0`的组合，依赖既有local JSONL/capacity原子写合同，Alembic不得猜测或扫描未受约束路径。该类 tree SHALL 原样保留 `0014`/`0015` 历史，不建立 `0016` ledger/claim/allocation，也不得在升级后恢复任何新 operation。

其余仍需继续执行或恢复的 `snapshot_backfill_required` tree，其 root ledger snapshot SHALL包含owner hard token/cost limits、cost-disabled状态、registry/config/catalog versions，以及root source和当时允许targets各自的descriptor/model-policy/target-budget/route/price sub-snapshot。Backfill bundle MUST 引用一个与自身记录ID不同的 durable immutable source checkpoint/evidence；该 source MUST 在 backfill bundle 之外保存创建/执行时的完整 snapshot 与 identity 基线，并能通过独立 record hash/version 校验。Migration MUST先解析该 source，再把bundle、source与versioned registry、descriptor/config history、price catalog逐值对照；bundle内部自带的snapshot/hash/version或相互一致字段 MUST NOT自证历史真实性。Migration-time current resolver、当前reload后配置、当前price、`0015` reservation数值、usage actual、默认值或零值 MUST NOT作为历史snapshot来源。Child MUST逐值继承同一owner snapshot ID与hard limits，并命中与自身target `agent_id`一致的sub-snapshot；source/target descriptor不同是合法常态，不得要求child复制source descriptor。

Legacy direct、delegation top-level与delegated child usage只有在独立durable source evidence同时提供各自ownership kind、stable semantic operation slot、tenant-scoped keyed request fingerprint及key version、tree/agent sub-snapshot refs与trusted bound时，才 MAY回填为具备exact replay语义的claim/allocation。Direct/allocation还必须提供实际route/price refs；child必须唯一绑定delegation claim。Delegation top-level必须唯一绑定`0015` relation/reservation，保存与`0015` normalized request hash同一canonical request bytes的fingerprint、target agent sub-snapshot、target frozen route/price catalog digest与可信top-level reservation bounds；provider/model/单一price/cache字段固定null。Migration MUST按当前change完全相同的对应canonical identity schema/算法重算并保存identity hash；缺少任一字段、fingerprint无法验证、top-level request hash与fingerprint来源不一致、delegation关联不唯一或hash/version冲突时 MUST整批fail closed，不得仅凭`usage_call_id`、`0015` request hash、provider result或当前配置猜测identity。Delegated child usage MUST绑定正确target sub-snapshot并建立allocation，MUST NOT建立direct claim。

在任何DDL/UPDATE前，migration MUST整批验证分类条件；每个需回填root的独立 source 引用必须存在、唯一且不同于backfill bundle，源记录可用，tenant/agent/registry/descriptor/config/catalog/hash/version一致，hard limits与root/targets允许route完整。Owner cost启用时，所有允许model route的input/output price与embedding route的input price MUST存在且非null、非bool、非负、有限；只有price key存在但值为null不算可解析。既不满足`legacy_closed`又缺少完整tree snapshot或direct/delegation top-level/allocation任一所需immutable identity、只能取得当前配置、bundle自证、多来源冲突、内容hash/version不符、cost-enabled price缺失/null/非法、child缺少对应target sub-snapshot或child evidence引用另一tree snapshot时 MUST整批fail closed。Source/target descriptor不同本身不得判冲突。维护流程 SHALL 先用旧 writer drain/reconcile 使无历史snapshot/identity的合法旧tree成为`legacy_closed`；不得以migration猜值替代该步骤。SQLite与PostgreSQL MUST逐值产生相同分类、snapshot、identity或拒绝。

#### Scenario: 无历史 snapshot 的封闭 legacy tree 可安全升级
- **WHEN** 合法 `0015` 数据库中的旧 root 没有 immutable snapshot 引用，但 root 具备上述dialect等价terminal closure proof，全部 usage/delegation/event-capacity/queue/approval/recovery 状态都满足 `legacy_closed`
- **THEN** `0016` 保留其 `0014`/`0015` 历史且不建立 shared ledger/claim/allocation；升级成功，reload 或新 writer 不得让该 tree 恢复新 operation

#### Scenario: 无历史 snapshot 的在途 tree 必须先 drain
- **WHEN** 旧 root 缺少 immutable snapshot，且仍非 terminal、存在 pending/needs-review/outstanding evidence，或不能逐值证明 `legacy_closed`
- **THEN** SQLite 与 PostgreSQL 都在 DDL/UPDATE 前整批拒绝，并要求旧 writer 先完成 drain/reconcile；不得用 current config、reservation 或 actual 合成 snapshot

#### Scenario: 可证明历史 snapshot 逐值回填
- **WHEN** root的backfill bundle引用不同记录ID的durable immutable source checkpoint/evidence，该source独立保存并证明唯一descriptor/config/catalog snapshot及允许route/price versions，且child relation与source逐值一致
- **THEN** `0016`先交叉验证source、bundle和versioned history，再回填hard limits、版本和route/price refs，child复用同一owner snapshot；迁移后reload不改变该root恢复、fallback或approval resume使用的snapshot

#### Scenario: Self-contained backfill bundle 不能自证
- **WHEN** 在途root只有一个包含完整snapshot、identity、hash与version且内部相互一致的backfill bundle，但未引用不同记录ID的durable immutable source checkpoint/evidence
- **THEN** SQLite与PostgreSQL都在DDL/UPDATE前整批拒绝；migration不得因为bundle字段自洽就把它当作创建时历史来源

#### Scenario: Cost-enabled null price 不能通过 snapshot 校验
- **WHEN** owner cost启用，snapshot中的model route input/output price或embedding route input price键存在但值为null，或值为bool、负数、NaN、Infinity
- **THEN** SQLite与PostgreSQL都在DDL/UPDATE前整批拒绝，不创建ledger且不把null解释为0或cost-disabled；同样snapshot在owner cost关闭时只跳过price上界要求，非法cost/status evidence仍按原合同拒绝

#### Scenario: 当前配置不能替代缺失历史版本
- **WHEN** `snapshot_backfill_required` root缺少immutable version引用、引用源已不存在/校验失败，或只能由migration-time current resolver取得配置和price
- **THEN** SQLite与PostgreSQL都在任何DDL/UPDATE前整批拒绝，不采用当前值、reservation、actual、默认值或零值；只有严格满足`legacy_closed`的tree可以不建立snapshot而保留旧历史

#### Scenario: Root 与 child tree snapshot 证据冲突
- **WHEN** child没有继承唯一budget owner的tree snapshot ID、缺少自身target `agent_id` sub-snapshot，或其durable target descriptor/model-policy/route/price version与该sub-snapshot不一致
- **THEN** migration在写入ledger/claim/allocation前整批拒绝，不读取current target配置或把冲突静默归一；若child正确引用同一tree中的独立target sub-snapshot，则source/target descriptor不同必须允许

#### Scenario: Legacy direct 缺少 immutable identity 时拒绝 backfill
- **WHEN** 在途root direct usage只有稳定`usage_call_id`和provider result，但缺少可信operation slot、keyed request fingerprint/key version、actual route/snapshot refs或trusted bound任一项
- **THEN** SQLite与PostgreSQL都在DDL/UPDATE前整批拒绝，不把`usage_call_id`当作完整identity；旧writer必须先drain到`legacy_closed`或补齐原本已durable存在的可验证证据，migration不得新造fingerprint

#### Scenario: Legacy delegation top-level 缺少 immutable identity 时拒绝 backfill
- **WHEN** 在途或待恢复`0015` delegation只有idempotency key、normalized request hash与reservation，但独立source缺少keyed request fingerprint/key version、owner/target snapshot、target route/price catalog digest或trusted top-level bound任一项
- **THEN** SQLite与PostgreSQL都在DDL/UPDATE前整批拒绝，不把`0015` request hash当作完整budget identity；migration不得从current snapshot/reservation或bundle自造identity，旧writer必须先drain到`legacy_closed`或提供原本已durable存在的source evidence

#### Scenario: Legacy delegation request hash 与 top-level identity 双重验证
- **WHEN** 独立source提供完整delegation top-level identity，但其keyed fingerprint不是对`0015` normalized request hash所用同一canonical request bytes生成，或target catalog/bounds与source snapshot和reservation不一致
- **THEN** migration在DDL/UPDATE前整批拒绝；只有request hash、fingerprint来源、tree/target catalog与trusted bounds逐值一致时才建立top-level claim

#### Scenario: Legacy child 缺少 allocation identity 时拒绝 backfill
- **WHEN** 在途delegated child usage只有`usage_call_id`与provider result，但缺少唯一delegation claim、target sub-snapshot、keyed request fingerprint/key version、actual route或trusted bound任一项
- **THEN** SQLite与PostgreSQL都在DDL/UPDATE前整批拒绝，不把child usage猜成allocation或direct claim；migration不得新造identity或把同一child result绑定到多个delegation

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
