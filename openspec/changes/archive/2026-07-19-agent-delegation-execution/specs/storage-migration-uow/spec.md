## ADDED Requirements

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
