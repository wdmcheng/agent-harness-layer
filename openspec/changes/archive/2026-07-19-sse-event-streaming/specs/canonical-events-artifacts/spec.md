## ADDED Requirements

### Requirement: CanonicalEvent 序号容量由 durable reservation 封闭
CanonicalEvent `seq` SHALL 使用 `1..2147483647` 的持久化范围，并消费 `0014` evidence outbox 建立的 run/operation capacity reservation。EventBus 与所有 sink MUST 在同一 run 的序号分配锁或事务内保证 `highest_persisted_seq + outstanding_reserved_event_count + terminal_reservation <= 2147483647`；`highest_persisted_seq` 是最大已持久化 `seq`，无 event 时为 `0`，MUST NOT 用 event row count 替代。预约消费、event 插入和 high-water mark 推进 MUST 在同一原子边界。terminal 只能消费 run 创建时的 terminal reservation，provider/tool/approval/delegation prerequisite evidence 只能消费各自副作用前建立的 operation reservation。容量不足的 operation MUST 在外部副作用前以稳定 `event.sequence_exhausted` fail closed，不消费 seq、不创建业务 evidence；未知结果保留 reservation并阻止 terminal。若既有状态违反容量不变量、high-water mark 与最大 seq 不一致，或 `seq=2147483647` 不是 terminal，任何新写入 MUST 以 `event.sequence_state_invalid` 零变更拒绝并要求人工处置，不得覆盖或删除 evidence。

#### Scenario: 容量不足在副作用前拒绝
- **WHEN** 新 operation 的最大 prerequisite event 预约会让 highest persisted seq、outstanding 与 terminal reservation 总和越过 `2147483647`
- **THEN** reservation 以 `event.sequence_exhausted` 零业务副作用失败，既有 operation 仍可消费自己的预约并在全部 prerequisite evidence 完成后由 terminal reservation 收口；SQLite/local 与 PostgreSQL 结果相同

#### Scenario: 稀疏高序号保留 terminal 容量
- **WHEN** run 已持久化 `seq={1, 2147483646}`，随后请求新的 operation reservation
- **THEN** 系统按 high-water mark `2147483646` 在副作用前拒绝 operation，terminal 仍可消费自己的最后一个预约；不得按两条 row 误判可用容量

#### Scenario: 非法最大序号状态拒绝继续写入
- **WHEN** 历史或直接数据库写入留下 `seq=2147483647` 的 non-terminal evidence
- **THEN** EventBus/sink 以 `event.sequence_state_invalid` 拒绝 terminal 和 non-terminal 新写入，既有 evidence、run 状态与序号均不改变

#### Scenario: 并发边界不产生部分写入
- **WHEN** PostgreSQL worker 或 local async caller 在序号预留边界并发发布 event
- **THEN** run 级锁/事务只允许容量不变量内的 reservation/event 提交，所有被拒绝 operation 都不消费 seq、不产生外部副作用、不留下未闭合 reservation
