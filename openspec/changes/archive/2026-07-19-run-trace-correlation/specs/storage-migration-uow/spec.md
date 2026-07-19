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

### Requirement: 已发布 0013 shape 漂移必须线性前滚
系统 MUST 保留已发布 `0013_run_trace_correlation` 的 revision 身份，并以唯一线性后继 `0013a_run_trace_event_hardening` 收敛现场旧 `0013` 与当前最终 `0013` 的事件 schema。普通运行入口 MUST 只接受 `0013a` head，不得仅因数据库 stamp 为 `0013` 就跳过物理 shape 修复。`0013a` MUST 在任何 DDL/DML 前精确区分两种允许来源：旧完整 shape 具有 `record_scope` 但缺 `stream_id`、tenant/stream 唯一键、scope CHECK、三列 run-owner FK、audit CHECK 与 agent run 三列引用键；最终完整 shape 已具备全部目标列和约束。旧 shape 必须先完整预检 scope、legacy stream、run/tenant/trace ownership、序列唯一性和 audit scope，再确定性把旧 `run_id` 复制为 `stream_id`、把 non-run 数据库 ownership 置空并补齐目标约束；最终 shape 只验证并 no-op。混合、部分或不兼容 shape MUST 在 mutation 前 fail closed。`0013a -> 0013` downgrade 只回退 revision stamp并保留硬化 schema和 evidence；真正的 `0013 -> 0012a` 破坏性回退继续由既有精确 opt-in 与空 evidence 门禁负责。后续 `0014` MUST 以 `0013a_run_trace_event_hardening` 为直接前置。

#### Scenario: 旧同名 revision 不再假通过
- **WHEN** 数据库 stamp 为 `0013_run_trace_correlation`，但仍是缺少 `stream_id` 与最终事件约束的旧完整 shape
- **THEN** 普通入口在创建 run、event 或其他业务副作用前报告 migration required；显式前滚到 `0013a` 后保留 legacy stream、正确分类 run/non-run ownership并开放写入

#### Scenario: Fresh 与旧库收敛到同一唯一 head
- **WHEN** fresh 数据库从 `0012a` 执行 `0013 -> 0013a`，或旧 service 数据库从已 stamp 的旧 `0013` 执行 `0013a`
- **THEN** 两条路径都得到相同最终列、唯一键、scope CHECK、三列 run-owner FK、audit CHECK 与唯一 Alembic head；partial shape 不产生任何 DDL/DML
