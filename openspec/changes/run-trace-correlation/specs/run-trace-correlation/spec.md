## ADDED Requirements

### Requirement: 每个新 run 在副作用前绑定 canonical trace
系统 SHALL 为每个新 root run 绑定一个非空、全局唯一的 canonical `trace_id`。调用方提供合法且未冲突的 trace 时 MUST 保留；缺失时 MUST 由受控 runtime composition 在创建 run、发布 lifecycle event、enqueue 或调用 tool/model/provider 前生成。任何下游组件 MUST NOT 为同一 run 生成第二个 trace。

#### Scenario: 缺失 caller trace 时生成
- **WHEN** 调用方从 API、CLI 或内部受控入口创建 run 且未提供 trace
- **THEN** 系统在首个持久化业务副作用前生成 canonical trace，并把后续 run context、event 与 approval 关联到该值

#### Scenario: 合法 caller trace 被保留
- **WHEN** 调用方提供合法且尚未绑定其他 root run 的 trace
- **THEN** 系统把该值绑定为 canonical trace，后续恢复、worker 和 evidence 不改写它

#### Scenario: 冲突 trace 零副作用失败
- **WHEN** caller trace 已绑定到另一个 root run 或不满足稳定格式
- **THEN** 系统返回结构化 validation/conflict 错误，且不创建 run、event、queue message、approval 或 provider side effect

### Requirement: canonical trace 跨恢复和进程边界保持不变
系统 SHALL 把 canonical trace 持久化在 run execution context，并让 local execution、service queue/worker、checkpoint/resume、approval/audit、tool/model evidence 和 terminal event 只从该上下文继承。不同入口请求可以使用不同 `request_id`，但同一 run 的 canonical `trace_id` MUST 保持不变。

#### Scenario: Worker 重建后恢复同一 trace
- **WHEN** API 提交 run 后由另一个 worker 进程读取持久化 execution context 并执行
- **THEN** queued、started、tool/model 与 terminal evidence 的 trace 与 run 创建时逐值一致

#### Scenario: Approval resume 不重新生成 trace
- **WHEN** waiting run 跨进程重启后被 approve 或 deny 并恢复 continuation
- **THEN** approval、audit、resume 和 terminal evidence 保留原 canonical trace，即使这些操作使用新的 request_id

### Requirement: 历史 nullable trace 数据确定性迁移
系统 SHALL 以 run 为单位为历史 nullable trace 数据执行确定性、幂等 backfill；同一 run 的 execution context、approval、run-scoped event/audit/trace record MUST 获得同一生成值，已有非空 trace MUST 保持不变。同一 run 已存在多个不同非空 trace、或记录无法唯一归属 run 时，migration MUST 在单事务内 fail closed，不得选择任一值、覆盖、部分提交或删除 evidence。

#### Scenario: 重复 backfill 结果一致
- **WHEN** migration 在相同历史数据上被验证或重试
- **THEN** 每个 run 得到相同 trace，已有 trace 不被覆盖，所有关联记录逐值一致

#### Scenario: 孤立记录阻止迁移
- **WHEN** nullable approval/event/audit record 无法唯一关联到现有 run
- **THEN** migration 整体失败并报告脱敏记录标识，不提交部分 backfill 或删除历史数据

#### Scenario: 冲突非空 trace 阻止迁移
- **WHEN** 同一历史 run 的 execution context、approval、event、audit 或 trace record 中存在两个及以上不同非空 trace
- **THEN** migration 整体失败并报告脱敏 run/record 标识，不选择 canonical 值、不覆盖已有值且不提交任何 backfill
