## ADDED Requirements

### Requirement: 模型工具循环使用既有 CanonicalEvent 目录与稳定顺序
模型工具 loop SHALL 复用既有 `model.request.started`/`model.usage.updated`、`tool.call.started|completed|failed`、`context.assembly.started|completed`、`approval.required|resolved` 事件类型，不新增等价 event。每个 event id 与 exact payload SHALL 绑定 loop id、turn ordinal、nullable tool call id 和对应 usage/result/context/approval refs，且不得包含 prompt、arguments、完整tool output、secret、SDK object或动态余额。

#### Scenario: Allow 工具轮事件顺序唯一
- **WHEN** 一轮 model intent被allow、工具完成并组装下一轮context
- **THEN** committed prerequisite顺序为model usage闭合后tool started→tool completed→context started→context completed
- **AND** 下一model started只出现在context completed之后

#### Scenario: Invalid或deny不伪造tool started
- **WHEN** intent validation、Registry resolve或policy deny在handler前停止
- **THEN** 不生产`tool.call.started|completed|failed`
- **AND** 只允许对应脱敏validation/policy/audit evidence

### Requirement: Event capacity 与 outbox 先于对应工具副作用
Runtime SHALL 从受信、版本化 operation-kind registry派生每个 model/tool/context/approval step 的最大 prerequisite event reservation，并在对应 provider/handler/外部副作用前通过run级锁/UoW原子预留。Reservation耗尽或state非法 MUST 零业务副作用拒绝；未知结果 SHALL 保留outstanding reservation并阻止terminal。

#### Scenario: Tool event容量不足零handler调用
- **WHEN** run剩余CanonicalEvent容量不足以容纳工具step与terminal
- **THEN** runtime在execution claim/handler前返回`event.sequence_exhausted`

#### Scenario: Tool完成但event发布未知保持围栏
- **WHEN** tool result已耐久但completed event/outbox确认未知
- **THEN** runtime不进入Context Assembly/next model turn或run terminal
- **AND** recovery只能补投同一stable event id
