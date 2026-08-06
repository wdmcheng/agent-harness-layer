## ADDED Requirements

### Requirement: 多轮模型usage绑定loop与turn身份
每个模型工具loop turn SHALL使用独立稳定`usage_call_id`并在started/final evidence中绑定loop id、turn ordinal、turn kind、catalog digest、nullable tool intent/tool call digest和loop frozen bounds。Turn ordinal MUST从1连续且不重复；final/cumulative evidence SHALL交叉验证provider attempts、token/cost/latency、shared-budget owner和loop next-turn state。Prompt、tool arguments/output和SDK object MUST NOT进入evidence。

#### Scenario: 三轮usage连续且可对账
- **WHEN**loop依次产生intent、intent和final三个model turns
- **THEN**三个usage identities分别绑定ordinal 1、2、3且全部actual累计到同一root/loop

#### Scenario: Turn identity漂移拒绝重放
- **WHEN**相同usage_call_id携带不同loop/ordinal/catalog/turn kind或intent digest
- **THEN**usage repository在provider前返回identity conflict

### Requirement: Model turn恢复遵守既有started与unknown语义
Recovery SHALL复用既有text/stream/route-chain/structured attempt proof和settlement状态判断model turn是否exact completed、可证明未开始或needs-review。Durable started、provider response/usage不完整、cleanup未知或commit acknowledgement未知 MUST NOT因loop retry被重新调用或记零；needs-review SHALL阻止后续tool/turn/terminal。

#### Scenario: 已完成intent turn不重调provider
- **WHEN**usage final已耐久但loop row尚未推进
- **THEN**recovery复用原ToolIntent digest和usage final并推进同一turn

#### Scenario: Started unknown阻止下一tool
- **WHEN**model turn有durable started但provider outcome无法证明
- **THEN**usage/loop进入needs-review且不生成ToolIntent或调用工具
