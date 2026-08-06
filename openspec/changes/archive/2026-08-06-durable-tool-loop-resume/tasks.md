## 1. 数据模型与迁移

- [x] 1.1 先补充迁移契约失败测试，断言当前 head 为 `0017_model_route_chain_state`、下一版本固定为 `0018_model_tool_loop_state`，并覆盖空库升级、既有数据升级、单行`model_tool_loop_schema_marker`初始false，以及冻结旧0017 migration catalog/binary面对SQLite与PostgreSQL 0018 head、新表或v1 evidence时在repository/worker/model/tool副作用前稳定拒绝且零数据改写；再新增迁移与启动门禁，完成时保存upgrade到head和旧binary refusal的验证证据。
- [x] 1.2 先为 `model_tool_loops` 的身份、精确 `active|waiting_approval|completed|failed|cancelled|needs_review` 状态、冻结边界、当前轮次、终态和版本约束补充数据库测试；再实现模型与约束，完成时证明重复身份、未知状态、非法转换和并发版本冲突均被拒绝。
- [x] 1.3 先为 `tool_invocations` 新增 loop/turn/`tool_call_id`/绑定、execution lease digest/fence/expiry、handler started与not-started proof字段补充兼容、唯一性和封闭状态测试，保留既有 `approval_id` 唯一约束；再实施扩展，完成时证明普通与审批路径共享 `tool_call_id` 至多一次认领且旧记录仍可读。
- [x] 1.4 先为 `context_assemblies` 新增 loop/turn/`tool_call_id`/输入输出摘要及新循环身份唯一约束补充测试；再实施扩展，完成时证明重放不会重复组装且旧记录不被错误纳入新唯一键。
- [x] 1.5 先补充 downgrade 拒绝测试，证明首条v1 evidence与schema marker同UoW提交、marker只能false→true，且删除/置空/导出业务证据不能绕过；再实现显式拒绝门禁，并验证marker仍false且扫描无v1 evidence时的唯一可逆路径与错误信息。

## 2. 耐久状态机与至多一次执行

- [x] 2.1 先为循环创建、轮次提交、等待审批、继续、终止和失败转换补充公开仓储测试；再实现带版本栅栏与租约的耐久状态机，完成时保存合法/非法转换和并发竞争证据。
- [x] 2.2 先为 `tool_call_id` 认领补充崩溃窗口测试，覆盖无claim、`claimed`活跃/过期lease、换租CAS竞争、`claimed→executing`提交确认未知、`executing`、已有结果和绑定不匹配；再让普通与审批执行共同使用 `claimed|executing|completed|failed|needs_review`、lease/fence 与一次性 `ToolExecutionPermit`，完成时证明 handler 对同一身份至多执行一次。
- [x] 2.3 先为终态联合栅栏补充并发测试，覆盖取消、预算耗尽、审批恢复、工具完成和模型最终回答竞争；再实现单一提交点，完成时证明只有一个终态胜出且败方不产生后续副作用。

## 3. 精确恢复与人工处置

- [x] 3.1 先补充精确结果恢复测试，证明已有工具结果可按输入/输出摘要和绑定重新投影为同一不可信上下文且不再执行 handler；再实现 exact-result replay，完成时保存相同上下文身份和零重复执行证据。
- [x] 3.2 先补充 `tool-handler-not-started-v1` 公开仓储测试，逐值断言 tool_call/binding、prior/next fence、旧lease expiry、固定reason与canonical proof digest；再实现仅限`claimed`且旧lease过期的owner UoW/CAS换租，完成时覆盖活跃lease拒绝、双worker竞争、旧owner越栅栏失败及`executing`绝不降级接管。
- [x] 3.3 先补充不确定执行状态、未知 schema/version/event、摘要不匹配和证据缺失测试；再实现 `needs_review` 失败关闭分支，完成时证明系统不会猜测成功、重放执行或跳过审计。
- [x] 3.4 先为审批后恢复补充绑定与新鲜度测试，覆盖批准、拒绝、过期、撤销和 grant 已消费；再复用现有 continuation/approval 接缝，完成时证明审批与普通工具身份收敛到同一耐久循环状态。

## 4. 用量、预算与事件重建

- [x] 4.1 先补充模型回合用量重放测试，证明既有 usage outbox 以稳定身份幂等记账且不引入第二张用量表；再接入循环轮次身份，完成时证明崩溃恢复不会重复 token 或成本。
- [x] 4.2 先补充共享父预算联合测试，覆盖模型 token/cost、工具副作用已知/未知 evidence、并发子运行、预算保留/结算和恢复后的剩余额度；再扩展现有 ledger 关联，完成时证明模型总额不超限，工具未知影响保留独立围栏且终态后不可继续消费；本 change 不引入工具计价或费用结算。
- [x] 4.3 先补充事件重建测试，覆盖循环、轮次、工具调用、审批、上下文和终态关联，并验证未知事件版本失败关闭；再实现耐久 CanonicalEvent 生产与读取，完成时证明事件可追溯但不会被误当成执行幂等唯一来源。

## 5. 回归、迁移与架构验证

- [x] 5.1 在SQLite与真实PostgreSQL上运行migration upgrade、冻结旧0017 binary/catalog对0018的启动拒绝、legacy compatibility、evidence-aware downgrade、仓储并发、崩溃注入、取消、预算、审批和上下文恢复测试；完成时保存命令、退出码与测试计数，并证明旧binary零数据改写且测试使用fake provider/tool、没有真实外部调用。
- [x] 5.2 运行全量受影响测试、类型检查、架构依赖守卫和现有 `runtime-checkpoint-runs`、审批、用量、预算回归；完成时证明新增持久化只位于允许的 adapter/persistence 边界。
- [x] 5.3 更新受影响的公开 API、迁移与运维文档，核对 `REQ-029`、`AC-108`、`AC-109`、`AC-110`、`AC-111`、`MOD-006` 和本 change 场景逐项可追踪；完成时执行文档契约测试并保存通过证据。
