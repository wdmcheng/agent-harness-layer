## ADDED Requirements

### Requirement: 工具循环approval恢复与durable loop/claim共同围栏
Approval resume SHALL在任何`run.resumed` event、tool claim takeover、handler或model调用前，交叉验证active resolution lease、ApprovalGrant、checkpoint、model_tool_loops row和nullable existing tool invocation claim。Loop/turn/tool-call/catalog/arguments/schema/action/resource、tenant/identity/run/request/trace、frozen bounds和approval lease/fingerprint MUST逐值一致。Existing completed/failed claim SHALL只返回exact结果；executing/needs-review claim SHALL拒绝takeover和handler重放。Existing claimed claim只有在tool execution lease已过期、binding一致，且owner UoW按`tool-handler-not-started-v1`原子换租并递增fence后才能继续；approval lease不能替代tool execution lease/fence。

#### Scenario: Matching grant与空claim恢复一次
- **WHEN**waiting loop、checkpoint、grant/lease匹配且不存在tool claim
- **THEN**runtime以原tool_call_id创建唯一claim并执行一次

#### Scenario: Existing completed claim只补approval evidence
- **WHEN**tool result已completed但approval resolved/terminal evidence尚未published
- **THEN**recovery返回既有result并只补ordered evidence
- **AND**handler与model调用计数不增加

#### Scenario: Existing executing claim阻止takeover
- **WHEN**approval lease过期但同tool_call_id claim仍executing或needs-review
- **THEN**API/worker返回resolution in progress或execution needs review
- **AND**不得换发能执行第二次handler的新lease/tool call identity

#### Scenario: Approved claimed claim按工具栅栏安全接管
- **WHEN**approval grant仍匹配且existing tool claim为`claimed`、tool execution lease已过期
- **THEN**runtime以同一tool_call_id原子写入可信未开始proof并轮换tool lease/fence
- **AND**旧owner和approval lease本身都不能绕过新的tool fence调用handler

### Requirement: Approval等待与恢复不重置loop边界
Approval record/checkpoint SHALL保存原loop frozen bounds和next ordinal identity。Resume时runtime SHALL复用原deadline、turn/count、catalog和累计usage，并重新检查current hard policy/owner balance；current config或reviewer decision MUST只能进一步拒绝，不能提高上限或改写下一step。

#### Scenario: 等待跨过deadline后拒绝
- **WHEN**approval通过时原loop absolute deadline已到达
- **THEN**resume在tool claim/handler前以limit/cancelled稳定终态关闭
- **AND**不创建新loop或延长deadline
