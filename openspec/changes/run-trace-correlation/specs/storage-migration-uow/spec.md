## ADDED Requirements

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
