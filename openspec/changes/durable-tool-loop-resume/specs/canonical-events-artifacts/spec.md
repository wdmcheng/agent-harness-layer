## ADDED Requirements

### Requirement: 工具循环event与outbox按稳定子身份exact replay
每个model/tool/context/approval step SHALL以loop id、turn ordinal、nullable tool call id及对应owner ref生成稳定event id和exact envelope。Outbox状态只允许按既有started/result_persisted/published/cancelled规则单调推进；相同event id只接受字节等价语义。Recovery SHALL只补投persisted exact envelope，MUST NOT从current payload/config重构、生成别名或重复业务event。

#### Scenario: Event发布失败只补投同一envelope
- **WHEN**tool/context result已耐久而event publish失败
- **THEN**recovery读取outbox并补投相同event id/payload/checksum
- **AND**不重调handler、ContextAssembler或model

#### Scenario: 同event id语义漂移被拒绝
- **WHEN**recovery以相同event id提交不同loop/turn/ref/digest/status
- **THEN**EventBus在artifact materialize/fan-out前返回replay conflict

### Requirement: Loop terminal需要全部owner evidence闭合
Run terminal publication SHALL在同一run锁定并校验loop state、所有model usage outbox、tool claims/final events、context assembly events、approval ordered evidence、shared budget和outstanding capacity。任一未决或needs-review SHALL保留terminal reservation；terminal一旦published，任何晚到loop event MUST拒绝。

#### Scenario: Context completed event缺失阻止terminal
- **WHEN**final model result已耐久但上一turn的context completed outbox仍未published
- **THEN**terminal validator拒绝并先补投context event

#### Scenario: Needs-review不发布伪终态
- **WHEN**provider/tool/commit outcome未知导致loop needs-review
- **THEN**不发布tool completed、model final、loop completed或run terminal
- **AND**outstanding capacity保持围栏
