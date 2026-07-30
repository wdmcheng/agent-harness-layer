## ADDED Requirements

### Requirement: 流式输出使用版本化容量操作与有序占位
事件容量注册表 SHALL 新增版本化、受信任的 `model_stream` 操作种类，其固定容量为 65。系统 MUST 在 provider 副作用前为同一 `model-stream:{usage_call_id}` group 创建 64 个 delta 占位和 1 个 completed 占位；该 group id 长度固定为 77，每个占位占用一个容量槽位并具有稳定顺序。只有绑定到该 group 且 payload 与稳定身份一致的事件才可消费预留。正常结束时，未使用 delta 占位 MUST 原子取消并释放等量未消费容量；释放只递减 outstanding，不推进 high-water。unknown 时不得释放。

#### Scenario: 使用少于 64 个 delta
- **WHEN** 成功调用实际使用 3 个 delta
- **THEN** 3 个 delta 与 1 个 completed 各消费一个槽位
- **AND** 其余 61 个 delta 占位被取消并恰好释放 61 个容量槽位
- **AND** stream 操作的 outstanding 最终为 0，high-water 只按 4 条真实事件推进

#### Scenario: 非法事件试图消费流容量
- **WHEN** event id、group、sequence、event type 或 payload identity 不匹配预建占位
- **THEN** sink 关闭失败且不递减 `model_stream` outstanding
- **AND** 不写入 CanonicalEvent，不把占位标记为 published

### Requirement: 流式未决证据阻止运行终态
只要某个 run 存在 `model_stream` 或关联 `model_usage` 的未决容量、`started`/`result_persisted` outbox、unknown 调用或未完成结算，系统 MUST 拒绝发布任何 run terminal 事件。只有已用事件全部发布、未用占位已取消、未消费容量已释放、用量最终证据已发布且预算与 provider lease 已结算后，终态围栏才可打开。

#### Scenario: unknown 流阻止终态
- **WHEN** provider 副作用已开始且调用被分类为 unknown
- **THEN** `run.completed`、`run.failed` 与 `run.cancelled` 均被拒绝
- **AND** 已持久化 delta 仍可读取，未决占位和预留保持可审计

#### Scenario: 所有证据完成后允许终态
- **WHEN** completed 和最终 usage 已发布，未使用占位已取消，容量、预算和 lease 均完成结算
- **THEN** 运行终态可以按既有终态协议发布
