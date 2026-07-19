## ADDED Requirements

### Requirement: Delegation 在副作用前消费 event capacity reservation
`DelegationService` SHALL 在创建 child run、投递 queue、调用 provider 或发布 delegation 业务 event 前，通过 `0014` durable evidence outbox 的受信、版本化、封闭 registry 以 `operation_kind=delegation` 派生最大 prerequisite event 数，并在 run 级锁或等价 CAS 内持久化 event capacity operation/reservation。调用方、tool/module payload 与 service queue message MUST NOT 提供、覆盖或缩小预约数。全新 delegation claim、parent budget reservation 与 event capacity operation/reservation MUST 在同一 application UoW 内提交或回滚；同一 idempotency key/hash 重放 MUST 复用首次持久化的 operation 和预约，MUST NOT 再次占用容量。

容量不足 MUST 在任何 child、queue、provider 或业务 event 副作用前以内部稳定错误 `event.sequence_exhausted` fail closed，且不得消费 `seq`。只有对应 prerequisite evidence 已持久化或能证明不再产生时才可按实耗结算或释放预约；结果未知时 MUST 保持 event capacity reservation 与 parent budget reservation 占用并阻止 parent terminal。Local 与 PostgreSQL/Redis service 路径 MUST 使用同一 application seam 并产生相同结果。

#### Scenario: 容量不足时 delegation 零副作用
- **WHEN** 全新 delegation claim 的最大 prerequisite event 数会使 `highest_persisted_seq + outstanding_reserved_event_count + terminal_reservation` 超过上限
- **THEN** `DelegationService` 在 claim、budget/event reservation、child、queue、provider 与业务 event 产生前以 `event.sequence_exhausted` 拒绝；既有容量、高水位和 terminal reservation 不变

#### Scenario: 同 key 重放不重复预约
- **WHEN** 相同 idempotency key/hash 在首次 claim、budget reservation 与 event capacity operation 已提交后重试或由 worker reclaim
- **THEN** local 与 service 路径复用首次持久化的 delegation operation、budget reservation 和 event capacity reservation，不创建第二个 child、queue operation、provider call 或容量预约

#### Scenario: 未知结果保持两类预约并阻止 terminal
- **WHEN** delegation 已产生外部副作用，但 child、queue、provider 或最终 evidence 的结果不确定
- **THEN** parent budget reservation 与 event capacity reservation 保持 reserved/needs_review，parent 不发布 terminal；恢复只能继续既有稳定 operation 或补投确定 evidence，不能把未知预算或 event 数按零释放

### Requirement: Delegation 使用固定的 CanonicalEvent 生命周期
获准的真实 delegation SHALL 在 parent run 上发布固定的 CanonicalEvent 生命周期，最多为 `delegation.claimed` -> `delegation.child.created` -> `delegation.completed|delegation.failed` 三条，final 两种类型互斥。child 创建前的确定性执行失败 SHALL 只发布 claimed 与 failed；edge、policy、tenant、cycle、depth、budget、idempotency 或 event-capacity 拒绝 MUST NOT 发布 delegation 业务事件。结果未知或 evidence 非法时，系统 MUST 保持 budget/event reservation 为 reserved/needs_review、阻止 parent terminal，且 MUST NOT 发布 completed/failed final。

四种事件 MUST 使用 parent `run_id`、parent canonical `trace_id`、source `agent_id`，并固定 `record_scope=run`、`visibility=internal`、`terminal=false`。event id MUST 分别为 `delegation:{delegation_id}:claimed`、`delegation:{delegation_id}:child` 与 `delegation:{delegation_id}:final`。重试、恢复和 worker reclaim MUST 复用或补投这些稳定 event id，MUST NOT 增加生命周期事件数或产生公开别名。

公共 payload MUST 只包含 `delegation_id`、`source_agent_id`、`target_agent_id`。claimed 只增加 `status=claimed`。child.created 增加 `child_run_id` 与 `status`，status MUST 只允许 `queued|running|completed|failed`；local inline 路径允许 attach 时 child 已终态。completed 增加 `status=completed` 与严格符合 API Contract 5.30 `DelegationSummary` 的完整脱敏 `summary`，MUST NOT 增加顶层 `child_run_id` 或 `error_code`。failed 增加 `status=failed` 与 `error_code=delegation.execution_failed`；只有 child 已创建时才携带严格符合 5.30 的完整脱敏 `summary`，child identity 只通过 `summary.children` 表达且 MUST NOT 另加顶层 `child_run_id`；pre-child failed 不得携带 `child_run_id` 或 `summary`。payload MUST NOT 包含 child input、完整 identity/request hash、动态余额、原始 usage、resume token、secret、本地路径或原始异常。

固定 CanonicalEvent catalog MUST 与 39 种生产枚举精确相等。`terminal=true` SHALL 当且仅当 event type 为 `run.completed`、`run.failed`、`run.cancelled`，且三种 run terminal event MUST 为 `visibility=public`；其他 event type MUST 为 `terminal=false`。EventBus 与 local/PostgreSQL sink MUST 在分配 seq、消费容量、物化 artifact 或 fan-out 前拒绝 type/terminal/visibility 不一致的 envelope。RUN-003、CLI 与 RUN-006 MUST 默认过滤 internal event；只有通过 tenant/run 授权并显式请求 internal visibility 的 reader 才能读取原始事件。

#### Scenario: 成功 delegation 发布三阶段事件
- **WHEN** child 已创建并以可信 evidence 完成 parent aggregation
- **THEN** parent run 恰有 claimed、child.created、completed 三条 delegation 事件，按 seq 严格有序，使用稳定 event id、parent trace/source agent 与受控 payload；三条均 internal 且 non-terminal

#### Scenario: child 创建前确定性失败
- **WHEN** claim 与预约已提交，但 child 创建前发生确定性执行失败
- **THEN** parent run 只有 claimed 与 failed，failed 使用 final event id、稳定 error_code，且不包含 child_run_id 或 summary；未消费的 child event capacity 按既有 outbox 规则结算或释放

#### Scenario: unknown 结果不伪造 final
- **WHEN** child、queue、provider 或 evidence 结果未知，或者 usage evidence 非法
- **THEN** delegation 保持 needs_review 和两类 reservation，parent terminal 被阻止，不发布 completed 或 failed，恢复不重放外部副作用

#### Scenario: terminal 组合在副作用前双向拒绝
- **WHEN** 非 run-terminal event 设置 `terminal=true`，或者三种 run-terminal event 之一设置 `terminal=false` 或 non-public visibility
- **THEN** EventBus 与 local/PostgreSQL sink 在 seq、容量、artifact 和 fan-out 变化前拒绝，既有事件和预约状态保持不变

#### Scenario: reader 默认隐藏 internal delegation evidence
- **WHEN** 普通 RUN-003、CLI 或 RUN-006 reader 未显式请求并通过 internal visibility 授权
- **THEN** 四种 delegation lifecycle event 均不返回；获准 internal reader 返回同一 CanonicalEvent，不生成别名或第二套事件
