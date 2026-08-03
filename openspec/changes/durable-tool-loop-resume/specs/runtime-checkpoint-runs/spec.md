## ADDED Requirements

### Requirement: 模型工具checkpoint只恢复同一loop步骤
模型工具 loop 的 checkpoint SHALL 使用版本化 exact state绑定loop id、request/catalog/bounds digests、turn ordinal、model usage call、tool call、approval/context refs、next allowed step和execution identity。Resume token只用于查找checkpoint，不构成工具或模型授权；runtime SHALL从durable loop row与各owner repository重算并逐值校验。缺失、额外、类型漂移、跨tenant/run、stale ordinal或同步篡改 MUST在 `run.resumed` event和任何副作用前拒绝。

#### Scenario: Exact checkpoint恢复next step
- **WHEN** matching identity持有合法resume token且durable owner states与checkpoint一致
- **THEN** runtime从checkpoint声明的唯一next step继续并复用原子身份

#### Scenario: 原始token不能替代approval grant
- **WHEN** approval-gated tool checkpoint只有resume token而无active matching grant/lease
- **THEN** resume在run.resumed/tool claim/handler前返回invalid transition

#### Scenario: 双份同步篡改仍失败
- **WHEN** caller同步改写checkpoint与approval metadata但与model_tool_loops canonical preimage不一致
- **THEN** runtime重算后拒绝且不发布resumed event

### Requirement: Worker recovery 不重放已开始模型或工具副作用
Startup/runtime recovery SHALL先读取loop、usage、tool claim、context、approval和outbox owner state，再决定exact replay、可信继续或needs-review。它 MUST NOT仅凭checkpoint kind、run status、queue redelivery或DBOS workflow retry重新调用provider/handler。Tool claim为`claimed`时只能在原execution lease过期后，以CAS原子保存`tool-handler-not-started-v1`、轮换lease digest并递增fence；`executing`不得接管。旧owner在lease/fencing失效后 MUST在`claimed→executing`提交和handler边界前停止。

#### Scenario: Queue redelivery复用同一loop
- **WHEN** 同一queued run被reclaim并已有durable loop/turn state
- **THEN** worker恢复原loop且不创建第二loop或重调已开始副作用

#### Scenario: 旧worker越过fence失败
- **WHEN**新worker取得合法owner后旧worker尝试推进相同turn
- **THEN** repository CAS/lease拒绝旧owner且结果不被覆盖
