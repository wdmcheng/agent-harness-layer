## ADDED Requirements

### Requirement: Evidence outbox migration downgrade 不删除结算事实
Alembic revision `0014` SHALL 只在 SQLite/PostgreSQL 数据库不存在任何 usage settlement、approval resolution、terminal、event capacity reservation 或其他 `run_evidence_outbox` evidence，且操作者显式传入 Alembic `-x allow_empty_evidence_downgrade=true` 时允许 downgrade 到 `0013`。参数缺失、重复、值不是精确小写 `true` 或存在任一历史/活跃 outbox、settlement、capacity evidence 时，downgrade MUST 在 DDL 前 fail closed、保留兼容读取且不得删除、重排或伪造 evidence；SQLite 与 PostgreSQL MUST 遵守相同结果。

Upgrade MUST 在 API/worker writers 已停的窗口，先完整预检既有 run/event/checkpoint/approval/tool durable state。已有 terminal 的 run 不建立预约；每个非 terminal run MUST 建立一个 terminal reservation，并把该 run 已持久化的最大 `seq`（无 event 时为 `0`）回填为可信 high-water mark，不能使用 event row count。只有能从持久化状态映射到封闭、版本化 `operation_kind` registry 的活跃 operation 才能按对应最大 prerequisite event 数回填 outstanding reservation；未知 operation kind、矛盾状态、已有 seq 越界、high-water mark 与最大已持久化 `seq` 不一致，或 `highest_persisted_seq + outstanding + terminal` 超限 MUST 在任何 DDL/UPDATE 前整批 fail closed。完成后 repository MUST 以数据库约束或同事务 CAS 维护 high-water/outstanding/terminal 容量不变量，并在同一 run 锁/事务内消费预约、插入 event 和推进 high-water mark。

#### Scenario: 空且可丢弃数据库允许回退
- **WHEN** 操作者对不存在任何 outbox/settlement/capacity evidence 的数据库以 `-x allow_empty_evidence_downgrade=true` 执行 `0014 -> 0013` downgrade
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
