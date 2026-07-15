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
