## ADDED Requirements

### Requirement: 模型工具循环使用可复算的耐久身份和封闭状态
系统 SHALL 从受信 tenant/run/agent/request/trace 与原始 operation key 派生稳定 loop id，并为每个 turn、model usage call、tool call、approval/checkpoint、Context Assembly 和 event/outbox 派生唯一子身份。`model-tool-loop-state-v1` SHALL 保存 request/catalog digests、冻结上界、status、next turn、累计 usage、当前 refs 与最终 result/error。状态只允许 `active|waiting_approval|completed|failed|cancelled|needs_review`，字段组合、transition 和 ordinal MUST 封闭且单调。

#### Scenario: 相同请求竞争同一 loop
- **WHEN** 两个worker并发以相同受信context和operation key创建loop
- **THEN** 只有一个stable loop row提交，另一方读取并验证同一identity
- **AND** 不创建第二个model/tool副作用

#### Scenario: 相同key不同请求发生冲突
- **WHEN** 相同loop id候选携带不同request/catalog/bounds identity
- **THEN** runtime在provider/tool/context副作用前返回`model.tool_loop_replay_conflict`

#### Scenario: 非法状态倒退被拒绝
- **WHEN** persisted loop从waiting/completed/needs-review被调用方改写为更早active turn
- **THEN** repository拒绝transition且不调用任何副作用

### Requirement: 恢复只允许 exact result、可信未开始或 needs-review
每个恢复点 SHALL 只产生三类处置：复用 exact durable result；在对应 owner 的耐久证据证明尚未开始时以同一 identity继续；或在未知/冲突时进入 needs-review。系统 MUST NOT 根据缺行、异常文本、当前config、caller flag或“看起来没有结果”推断副作用未开始。

#### Scenario: 已完成 model/tool/context 全部复用
- **WHEN** model usage final、tool result和Context Assembly均已耐久但worker在下一状态提交前退出
- **THEN** recovery逐项复用原refs/digests并从next ordinal继续
- **AND** provider、handler和Context Assembly create计数不增加

#### Scenario: Tool executing 无结果进入needs-review
- **WHEN** tool claim已进入executing且没有可信未开始proof或completed result
- **THEN** loop进入needs-review并保留claim/budget/capacity
- **AND** handler与model均不重调

#### Scenario: 缺失当前配置不能重建旧identity
- **WHEN** current Agent/tool catalog或bounds与durable loop snapshot不同
- **THEN** recovery只按耐久snapshot校验或以conflict关闭
- **AND** 不采用更宽current配置

### Requirement: 所有恢复路径保持全局边界和共享预算
Loop 的 max turns、total tokens、nullable total cost、tool output bytes、absolute deadline和catalog identity SHALL 在首次副作用前耐久冻结。每个model turn SHALL使用同一root execution-tree ledger的稳定usage claim；累计actual/reservation与loop剩余量交叉校验。Approval、restart、worker reclaim、config reload和exact replay MUST NOT重置ordinal、deadline、reservation或已结算impact。

#### Scenario: Restart不重置turn或deadline
- **WHEN** worker在第n轮后重启
- **THEN** next turn仍为n+1且使用原absolute deadline和剩余预算

#### Scenario: Exact replay不重复扣减
- **WHEN** 已结算model turn或tool zero/known impact被再次恢复
- **THEN** owner ledger与loop cumulative保持原值且不创建第二reservation/actual

#### Scenario: Unknown影响保留围栏
- **WHEN** 任一model/tool impact无法证明完整
- **THEN** loop/shared-budget保持needs-review/reserved且新的turn被拒绝

### Requirement: 联合 evidence 未闭合前禁止 run terminal
Run terminal SHALL 只在 loop terminal state确定、全部model usage settlements、tool claims/results、Context Assemblies、approval ordered evidence、CanonicalEvent outbox和shared-budget owner状态逐值一致且无未决项时发布。任一 executing、waiting、needs-review、unknown、result_persisted未发布或identity conflict MUST保留terminal reservation并阻止terminal。Terminal一旦提交，新的turn/tool/context写入 MUST失败。

#### Scenario: 未决tool claim阻止terminal
- **WHEN** loop看似得到final model result但前一tool claim仍executing/unknown
- **THEN** runtime不得发布loop completed或run terminal

#### Scenario: Event补投后唯一terminal
- **WHEN** 所有业务结果已耐久但某个stable event尚未published
- **THEN** recovery只补投相同envelope，全部prerequisite完成后发布唯一terminal

#### Scenario: Terminal后拒绝晚到写入
- **WHEN** run terminal已持久化后旧worker尝试写turn/tool/context event
- **THEN** repository/EventBus在写入和外部副作用前拒绝

### Requirement: 0018迁移保持旧数据兼容并对新evidence降级失败关闭
系统 SHALL 以 `0018_model_tool_loop_state` 从0017增加loop协调表、单行`model_tool_loop_schema_marker`以及tool invocation/context assembly的nullable v1 identity字段。Marker exact key SHALL为`model-tool-loop-v1`，`evidence_seen`初始false；首条loop row或tool/context v1 identity写入 MUST在同一UoW先把它单调置true，应用与维护入口 MUST NOT清零或删除marker。SQLite与PostgreSQL schema、unique constraints和repository语义 MUST一致。Legacy rows SHALL保持可读且不被伪造为v1 loop；只要marker为true或扫描发现任何v1 evidence，downgrade MUST拒绝，不能通过删除、置空或导出业务证据后降级。

#### Scenario: 空库与legacy rows升级
- **WHEN** 0017空库或仅含legacy tool/context/checkpoint rows升级到0018
- **THEN** 旧行为和数据逐值保留，新字段为null且migration head唯一

#### Scenario: 新loop evidence阻止downgrade
- **WHEN** 任一model_tool_loops row或tool/context v1 identity存在
- **THEN** 同一UoW已把schema marker单调置true，0018→0017 downgrade在删除schema前失败并保留全部evidence

#### Scenario: 删除业务证据不能清除降级历史
- **WHEN** marker已为true且调用方删除、置空或导出可见loop/tool/context业务证据后请求downgrade
- **THEN** immutable marker仍使downgrade失败，且没有受支持入口可把它改回false

#### Scenario: SQLite与PostgreSQL唯一竞争一致
- **WHEN** 两个并发writer竞争相同loop/tool/context identity
- **THEN** 两种数据库都只有一个winner，其余读取exact或得到identity conflict

#### Scenario: 旧binary拒绝0018数据库
- **WHEN** 冻结migration catalog head为`0017_model_route_chain_state`的旧binary面对current head为`0018_model_tool_loop_state`的SQLite或PostgreSQL数据库、新增loop表或任一v1 evidence
- **THEN** application、worker与维护入口均在repository访问、事件发布、model或tool副作用前以稳定migration-head错误启动失败
- **AND** 不改写revision、marker、loop、tool invocation、context assembly或其他业务数据
