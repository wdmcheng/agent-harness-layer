# canonical-events-artifacts Specification

## Purpose
定义 provider-neutral `CanonicalEvent`、事件顺序与唯一终态，以及大 payload 的 artifact/ref 边界，使 runtime、API、audit、observability 和 eval 可共享稳定且可脱敏的事件证据。
## Requirements
### Requirement: CanonicalEvent 统一 run event envelope
package SHALL 暴露 `CanonicalEvent` envelope，包含 `event_id`、`event_type`、`event_version`、`seq`、`timestamp`、`tenant_id`、`user_id`、`agent_id`、`run_id`、`parent_run_id`、`trace_id`、`span_id`、`visibility`、`payload`、`payload_ref`、`raw_event_ref` 和 terminal marker。

#### Scenario: Envelope 字段可跨 adapter 传递
- **WHEN** event bus 发布任意 CanonicalEvent
- **THEN** persisted event 使用稳定 envelope 字段名，不暴露 provider-specific event object 或 ORM model

#### Scenario: 同一 run 只能出现一个 terminal event
- **WHEN** event bus 已为某个 `run_id` 发布 terminal event
- **THEN** 后续对同一 `run_id` 发布 terminal event 会失败或被拒绝

#### Scenario: 同一 run 内 seq 单调递增
- **WHEN** event bus 连续为同一 `run_id` 发布事件
- **THEN** persisted events 的 `seq` 从 1 开始单调递增，且断线后可按 last `seq` 继续读取

### Requirement: P0 event type catalog 与 Product-Spec 对齐
package SHALL 暴露 Product-Spec P0 catalog 中的 CanonicalEvent type，包括 run、model、guardrail、reasoning、tool、retrieval、context assembly、policy、approval、checkpoint 和 artifact 事件。

#### Scenario: Guardrail blocked 使用规范事件名
- **WHEN** input guardrail 拦截外部输入
- **THEN** event type 使用 `input.guardrail.blocked`，而不是 provider 或实现自定义的 denied/allowed 命名

### Requirement: Local jsonl sink 永远可作为证据 fallback
package SHALL 提供 local jsonl event sink，在没有外部 observability provider 时仍能持久化 trace/eval/audit 证据。

#### Scenario: 未配置外部 provider 时写入 local evidence
- **WHEN** settings observability kind 为 `local-jsonl`
- **THEN** event sink 将 CanonicalEvent 以 JSON Lines 写入 configured path，并可按 `run_id` / `seq` 读取

### Requirement: Artifact store 承载大 payload
package SHALL 提供 artifact store，把超过 inline 阈值或标记为大 payload 的内容写入 artifact，并在事件正文中只保留 `payload_ref` 和 checksum。

#### Scenario: 大 payload 不进入事件正文
- **WHEN** publisher 发送超过 inline 阈值的 payload
- **THEN** event payload 为空或为摘要，事件包含 `payload_ref` 和 checksum，artifact store 可按 ref 读取原始 payload

### Requirement: Guardrail 和 context assembly 事件不泄漏 secret
guardrail/context assembly event payload MUST 只包含摘要、`source_ref`、`trust_level`、truncation metadata 和 decision，不写完整大 payload 或 secret。

#### Scenario: Secret-like 字段被 redaction
- **WHEN** guardrail/context event payload 包含 api key、password、token 或 secret-like 字段
- **THEN** persisted event 不包含原始 secret value

### Requirement: OTel mapping facade 不依赖 provider SDK
package SHALL 暴露 CanonicalEvent 到 OTel span/metric/event 的 mapping facade，但 core event modules MUST NOT import Logfire、Phoenix、Langfuse 或 OTel exporter SDK。

#### Scenario: Mapping facade 可用 fake event 测试
- **WHEN** tests 使用 fake CanonicalEvent 调用 OTel mapping facade
- **THEN** 返回 provider-neutral mapping DTO，不需要真实 observability provider 或 API key

### Requirement: Run-scoped CanonicalEvent 必须携带 canonical trace
EventBus SHALL 要求所有具有 `run_id` 的 lifecycle、approval、tool、model、retrieval、eval 和 terminal CanonicalEvent 携带该 run 的 canonical `trace_id`。Event sink MUST 拒绝同一 run 中缺失或与 persisted run context 不一致的 trace；非 run telemetry 不受此要求影响。

公开 CanonicalEvent envelope 的 `run_id` SHALL 继续表示事件 stream。关系库 MUST 以独立 typed ownership 表示真实 AgentRun：run scope 必须以可延迟的 `(run_id, tenant_id, trace_id)` 复合外键或严格等价数据库约束引用同一 AgentRun，不能只验证三个字段各自合法；non-run scope 的数据库 ownership `run_id` 必须为空，并以 tenant-scoped `stream_id` 保留 envelope stream 和分配 seq。系统 MUST NOT 为 non-run telemetry 创建 sentinel/fake AgentRun；按 `run_id` 的 repository read 只返回真实 run ownership，不得把跨租户同名 non-run stream 混入结果。

Local 与 PostgreSQL event sink MUST 只把稳定事件语义完全一致的相同 `event_id` 视为幂等重试。稳定指纹只排除 sink 分配的 `seq` 与调用方重建重试时不稳定的 `timestamp`；其余 envelope 字段任一不同都 MUST 在 artifact materialize 与 provider/fan-out 前返回不包含既有 payload 或归属信息的 replay conflict。状态已提交后的 terminal/approval 恢复 MUST 优先读取并校验既有确定性 evidence，只有缺失时才补写，不得用新请求的 `request_id` 改写或重构同一 event-id。

#### Scenario: 同一 run 的事件 trace 一致
- **WHEN** run 依次产生 queued、started、approval、tool/model 与 terminal event
- **THEN** 所有事件保留各自唯一 event_id/seq，同时共享同一 canonical trace

#### Scenario: 错误 trace 被拒绝
- **WHEN** 下游组件尝试发布带空 trace 或不同 trace 的 run-scoped event
- **THEN** EventBus 在持久化和 provider fan-out 前拒绝该事件并产生封闭诊断，不改写 canonical trace

#### Scenario: 相同 event-id 的不同事件语义被拒绝
- **WHEN** 调用方使用已持久化 event-id 发布不同 event type、payload/ref/checksum、identity、request/span、scope、terminal、visibility 或 run/tenant/trace 的事件
- **THEN** sink 返回脱敏 replay conflict，保留原事件且不创建 artifact、不执行 fan-out；仅 seq/timestamp 不同的同语义重试仍返回原事件

#### Scenario: Service PostgreSQL 保存 non-run telemetry

- **WHEN** service composition 的 TelemetryFacade 向 PostgreSQL sink 发布 `context.run_id=null`、`trace_id=null` 且 envelope stream 为 `telemetry` 的 ordinary telemetry
- **THEN** sink 在 tenant-scoped stream 上分配稳定 seq，持久化 `record_scope=non_run` 和空数据库 run ownership，保留 envelope stream，且不查询或创建 AgentRun lineage

### Requirement: Model usage CanonicalEvent 使用有界稳定 payload
model 与 embedding 调用都 SHALL 复用现有 `model.request.started` 和 `model.usage.updated` 两个 CanonicalEvent type，并以 `ModelUsageEvidence.usage_kind=model|embedding` 区分；系统 MUST NOT 为 embedding 擅自新增等价事件名。两个 event SHALL 复用 CanonicalEvent envelope 和同一稳定 `usage_call_id`。`usage_call_id` MUST 在 provider 副作用前生成，并以非空 string 固定写入 `CanonicalEvent.payload.correlation.usage_call_id`；TelemetryFacade MUST 把同一值保留在 `TelemetryRecord.payload.correlation.usage_call_id`。系统 MUST NOT 新增 CanonicalEvent envelope 顶层字段或使用其他 payload 路径，也 MUST NOT 把该值放入 `ModelUsageEvidence` public DTO。Payload 其余内容 MUST 只包含 provider-neutral usage/decision/outcome 摘要；prompt、embedding 原文、vector 全文、headers、secret、provider raw response/client 和 raw exception MUST 被脱敏、替换为安全 ref 或阻止写入。

同一 run 的 terminal event SHALL 是最后一条 CanonicalEvent，且 `run.completed`、`run.failed`、`run.cancelled` 的 `visibility` MUST 为 `public`。durable evidence outbox MUST 以稳定 event id和显式顺序先发布 usage、`approval.resolved` 等 prerequisite evidence，最后发布 public terminal；approval continuation 的确定性结果 MUST 在同一 ordered group 中把 resolution 排在 terminal 前，二者 durable 后才公开 resolution。EventBus 与 local/PostgreSQL sink MUST 在持久化前拒绝 `terminal=true` 且 `visibility!=public`，并对 terminal 后的 terminal 或 non-terminal 写入统一 fail closed，不能只拒绝第二个 terminal；terminal 可见后 recovery MUST NOT 再补写 prerequisite evidence或重放 provider/tool。

`0014` durable evidence outbox SHALL 为每个 run 持久化 terminal capacity reservation。任何可能产生后续 evidence 的 provider、tool、approval 或 delegation operation MUST 在外部副作用前，由受信、版本化、封闭的 typed registry 以 `operation_kind` 派生最大 prerequisite event 数，并在 run 级锁或等价 CAS 内原子建立 operation reservation；业务 agent、HTTP/CLI 输入和 provider payload MUST NOT 提供或缩小该数值。容量不变量 MUST 是 `highest_persisted_seq + outstanding_reserved_event_count + terminal_reservation <= 2147483647`；`highest_persisted_seq` 为该 run 最大已持久化 `seq`，无 event 时为 `0`，MUST NOT 用 event row count 代替。预约消费、event 插入与 high-water mark 推进 MUST 在同一 run 锁/事务完成。容量不足 MUST 以 `event.sequence_exhausted` 在副作用前拒绝且不消费 seq。Reservation 只在对应 evidence 已持久化或能证明不会产生时按实耗结算/释放；结果未知时 MUST 保持占用并阻止 terminal。Terminal MUST 消费既有 terminal reservation，不能挪用未结算 operation reservation。

正常持久化的 CanonicalEvent envelope MUST 使用公共 `canonical_event_bytes()` 计算：`CanonicalEvent.to_payload()` 经 UTF-8、`ensure_ascii=false`、排序键、紧凑 separators 与 `allow_nan=false` 编码，JSONL 换行和 SSE frame 开销不计入，并且结果不超过 `65536` bytes。EventBus MUST 先把超限 payload 写入 artifact/ref 并重算；重算后仍超限时以 `event.envelope_too_large` 在写入和 fan-out 前拒绝。local/DB sink、legacy 校验和 SSE byte page MUST 复用该 serializer。历史或 direct-write 超限 row MUST 以 `event.envelope_state_invalid` fail closed，不得截断或返回无 cursor 的空读取结果。

#### Scenario: Started 与最终 usage event 关联一致
- **WHEN** 同一次 model/embedding 调用发布 started 和调用级最终 usage event
- **THEN** 两个 event 的 tenant/run/request/agent/trace 与 `payload.correlation.usage_call_id` 逐值一致，TelemetryRecord 保留相同路径和值，各自 `event_id` 保持唯一，run 内 seq 单调递增，最终 usage 恰好一条且 `CanonicalEvent.terminal=false`

#### Scenario: 大或敏感 provider payload 不内联
- **WHEN** adapter 输入包含 prompt、embedding、vector、secret 或 provider raw response
- **THEN** CanonicalEvent payload 不包含原值，只保留有界脱敏摘要或允许的 artifact/ref

#### Scenario: Terminal 后拒绝晚到 usage
- **WHEN** 同一 run 已持久化 terminal event，随后收到任何 usage 或其他业务事件写入
- **THEN** EventBus/sink 在分配新 seq 和 fan-out 前拒绝写入，既有 terminal 和聚合 evidence 保持不变

#### Scenario: Non-public run terminal 在持久化前拒绝
- **WHEN** 任一 outbox、runtime 或恢复路径尝试发布 `run.completed`、`run.failed` 或 `run.cancelled`，但 envelope 为 `terminal=true` 且 `visibility!=public`
- **THEN** EventBus 与 local/PostgreSQL sink 在持久化、seq 消耗和 fan-out 前统一拒绝；调用方修正为 public terminal 后，默认 RUN-003、RUN-006 与 CLI reader 都能观察同一最终结算信号

#### Scenario: Approval resolution 与 terminal 按 outbox 顺序恢复
- **WHEN** approved tool 已持久化确定性结果，但 resolution 或 terminal sink 写入失败、确认丢失或进程退出
- **THEN** recovery 以稳定 event id先补投唯一 `approval.resolved`，再补投唯一 terminal，二者完成后才公开 approval resolution；tool handler 不重放

#### Scenario: 副作用前预约全部 prerequisite evidence
- **WHEN** provider/tool/approval/delegation operation 声明的最大 event 数会让 highest persisted seq、outstanding 和 terminal reservation 总和超过上限
- **THEN** repository 在任何外部副作用前以 `event.sequence_exhausted` 拒绝；没有 started/outbox/provider/tool/queue 业务副作用，已有预约和 terminal capacity 不变

#### Scenario: 稀疏高序号不能低估已用容量
- **WHEN** non-terminal run 已持久化 `seq={1, 2147483646}`，随后 operation 请求 prerequisite reservation
- **THEN** repository 以 `highest_persisted_seq=2147483646` 在副作用前拒绝 operation，保留 terminal reservation；SQLite/local 与 PostgreSQL 均不得按两条 row 计算

#### Scenario: 未知结果保持 event capacity
- **WHEN** 已预约 operation 产生外部副作用后结果或最终 evidence 不确定
- **THEN** reservation 保持 outstanding，run 不发布 terminal；恢复只补投稳定 evidence，不能把未知 event 数按零释放

#### Scenario: 超限 envelope 在写入前拒绝
- **WHEN** payload artifact 化后完整 CanonicalEvent envelope 仍超过 `65536` bytes
- **THEN** EventBus 以 `event.envelope_too_large` 零持久化、零 fan-out 失败，不截断 envelope或消耗 seq

#### Scenario: Canonical serializer 跨 sink 结果唯一
- **WHEN** envelope 包含中文、转义字符、不同键插入顺序、恰好边界或 NaN
- **THEN** EventBus、local JSONL、SQLite/PostgreSQL 校验和 SSE page 使用同一 canonical bytes；键顺序不改变计数，恰好 `65536` bytes 允许、超过一个 byte 拒绝，NaN 在持久化前拒绝

### Requirement: Delegation 在副作用前消费 event capacity reservation
`DelegationService` SHALL 在创建 child run、投递 queue、调用 provider 或发布 delegation 业务 event 前，通过 `0014` durable evidence outbox 的受信、版本化、封闭 registry 以 `operation_kind=delegation` 派生最大 prerequisite event 数，并在 run 级锁或等价 CAS 内持久化 event capacity operation/reservation。调用方、tool/module payload 与 service queue message MUST NOT 提供、覆盖或缩小预约数。全新 delegation claim、parent budget reservation 与 event capacity operation/reservation MUST 在同一 application UoW 内提交或回滚；同一 idempotency key/hash 重放 MUST 复用首次持久化的 operation 和预约，MUST NOT 再次占用容量。

容量不足 MUST 在任何 child、queue、provider 或业务 event 副作用前以内部稳定错误 `event.sequence_exhausted` fail closed，且不得消费 `seq`。只有对应 prerequisite evidence 已持久化或能证明不再产生时才可按实耗结算或释放预约；结果未知时 MUST 保持 event capacity reservation 与 parent budget reservation 占用并阻止 parent terminal。Local 与 PostgreSQL/Redis service 路径 MUST 使用同一 application seam 并产生相同结果。

#### Scenario: 容量不足时 delegation 零副作用
- **WHEN** 全新 delegation claim 的最大 prerequisite event 数会使 `highest_persisted_seq + outstanding_reserved_event_count + terminal_reservation` 超过上限
- **THEN** `DelegationService` 在 claim、budget/event reservation、child、queue、provider 与业务 event 产生前以 `event.sequence_exhausted` 拒绝；既有容量、高水位和 terminal reservation 不变

#### Scenario: 同 key 重放不重复预约
- **WHEN** 相同 idempotency key/hash 在首次 claim、budget reservation 与 event capacity operation 已提交后重试或由 worker reclaim
- **THEN** local 与 service 路径复用首次持久化的 delegation operation、budget reservation 和 event capacity reservation，不创建第二个 child、queue operation、provider call 或容量预约

#### Scenario: 未知结果保持两类预约并阻止 terminal
- **WHEN** delegation 已产生外部副作用，但 child、queue、provider 或最终 evidence 的结果不确定
- **THEN** parent budget reservation 与 event capacity reservation 保持 reserved/needs_review，parent 不发布 terminal；恢复只能继续既有稳定 operation 或补投确定 evidence，不能把未知预算或 event 数按零释放

### Requirement: Delegation 使用固定的 CanonicalEvent 生命周期
获准的真实 delegation SHALL 在 parent run 上发布固定的 CanonicalEvent 生命周期，最多为 `delegation.claimed` -> `delegation.child.created` -> `delegation.completed|delegation.failed` 三条，final 两种类型互斥。child 创建前的确定性执行失败 SHALL 只发布 claimed 与 failed；edge、policy、tenant、cycle、depth、budget、idempotency 或 event-capacity 拒绝 MUST NOT 发布 delegation 业务事件。结果未知或 evidence 非法时，系统 MUST 保持 budget/event reservation 为 reserved/needs_review、阻止 parent terminal，且 MUST NOT 发布 completed/failed final。

四种事件 MUST 使用 parent `run_id`、parent canonical `trace_id`、source `agent_id`，并固定 `record_scope=run`、`visibility=internal`、`terminal=false`。event id MUST 分别为 `delegation:{delegation_id}:claimed`、`delegation:{delegation_id}:child` 与 `delegation:{delegation_id}:final`。重试、恢复和 worker reclaim MUST 复用或补投这些稳定 event id，MUST NOT 增加生命周期事件数或产生公开别名。

公共 payload MUST 只包含 `delegation_id`、`source_agent_id`、`target_agent_id`。claimed 只增加 `status=claimed`。child.created 增加 `child_run_id` 与 `status`，status MUST 只允许 `queued|running|completed|failed`；local inline 路径允许 attach 时 child 已终态。completed 增加 `status=completed` 与严格符合 API Contract 5.30 `DelegationSummary` 的完整脱敏 `summary`，MUST NOT 增加顶层 `child_run_id` 或 `error_code`。failed 增加 `status=failed` 与 `error_code=delegation.execution_failed`；只有 child 已创建时才携带严格符合 5.30 的完整脱敏 `summary`，child identity 只通过 `summary.children` 表达且 MUST NOT 另加顶层 `child_run_id`；pre-child failed 不得携带 `child_run_id` 或 `summary`。payload MUST NOT 包含 child input、完整 identity/request hash、动态余额、原始 usage、resume token、secret、本地路径或原始异常。

固定 CanonicalEvent catalog MUST 与 39 种生产枚举精确相等。`terminal=true` SHALL 当且仅当 event type 为 `run.completed`、`run.failed`、`run.cancelled`，且三种 run terminal event MUST 为 `visibility=public`；其他 event type MUST 为 `terminal=false`。EventBus 与 local/PostgreSQL sink MUST 在分配 seq、消费容量、物化 artifact 或 fan-out 前拒绝 type/terminal/visibility 不一致的 envelope。RUN-003、CLI 与 RUN-006 MUST 默认过滤 internal event；只有通过 tenant/run 授权并显式请求 internal visibility 的 reader 才能读取原始事件。

#### Scenario: 成功 delegation 发布三阶段事件
- **WHEN** child 已创建并以可信 evidence 完成 parent aggregation
- **THEN** parent run 恰有 claimed、child.created、completed 三条 delegation 事件，按 seq 严格有序，使用稳定 event id、parent trace/source agent 与受控 payload；三条均 internal 且 non-terminal

#### Scenario: child 创建前确定性失败
- **WHEN** claim 与预约已提交，但 child 创建前发生确定性执行失败
- **THEN** parent run 只有 claimed 与 failed，failed 使用 final event id、稳定 error_code，且不包含 child_run_id 或 summary；未消费的 child event capacity 按既有 outbox 规则结算或释放

#### Scenario: unknown 结果不伪造 final
- **WHEN** child、queue、provider 或 evidence 结果未知，或者 usage evidence 非法
- **THEN** delegation 保持 needs_review 和两类 reservation，parent terminal 被阻止，不发布 completed 或 failed，恢复不重放外部副作用

#### Scenario: terminal 组合在副作用前双向拒绝
- **WHEN** 非 run-terminal event 设置 `terminal=true`，或者三种 run-terminal event 之一设置 `terminal=false` 或 non-public visibility
- **THEN** EventBus 与 local/PostgreSQL sink 在 seq、容量、artifact 和 fan-out 变化前拒绝，既有事件和预约状态保持不变

#### Scenario: reader 默认隐藏 internal delegation evidence
- **WHEN** 普通 RUN-003、CLI 或 RUN-006 reader 未显式请求并通过 internal visibility 授权
- **THEN** 四种 delegation lifecycle event 均不返回；获准 internal reader 返回同一 CanonicalEvent，不生成别名或第二套事件

### Requirement: Shared-budget claim 是 terminal 前置 evidence
Shared-budget reservation/settlement SHALL 与既有 usage/delegation evidence 关联，但 MUST NOT 复用或篡改 event capacity 数值。任一未结算 shared-budget claim MUST 阻止 run terminal。预算拒绝允许写入唯一、稳定、脱敏且封闭的内部 decision/audit/usage rejection evidence；provider、child、queue、业务执行与 delegation 生命周期 event 副作用 MUST 为零。

新 direct operation 的 shared claim、usage outbox 与 event-capacity reservation，以及新 delegation 的 shared claim、既有 delegation reservation、ordered evidence 与 event-capacity reservation，MUST 分别在同一 application UoW 全部提交或回滚。可信结果持久化、`side_effect_state=result_committed`与 shared settlement MUST 原子提交；cache hit的zero-impact claim/allocation、usage result/outbox与capacity结算也必须原子，不能产生单边记录。提交后的event publish失败只补投既有outbox，不得重放外部副作用。

#### Scenario: Budget 拒绝只留下允许的内部证据
- **WHEN** shared ledger 在外部副作用前拒绝 direct 或 delegation operation
- **THEN** 系统最多写合同允许的稳定内部 rejection evidence，不发布 delegation claimed/child/final，不调用 provider、不创建 child、不投递 queue，event capacity 与 token/cost ledger 分别保持各自不变量

### Requirement: CanonicalEvent 序号容量由 durable reservation 封闭
CanonicalEvent `seq` SHALL 使用 `1..2147483647` 的持久化范围，并消费 `0014` evidence outbox 建立的 run/operation capacity reservation。EventBus 与所有 sink MUST 在同一 run 的序号分配锁或事务内保证 `highest_persisted_seq + outstanding_reserved_event_count + terminal_reservation <= 2147483647`；`highest_persisted_seq` 是最大已持久化 `seq`，无 event 时为 `0`，MUST NOT 用 event row count 替代。预约消费、event 插入和 high-water mark 推进 MUST 在同一原子边界。terminal 只能消费 run 创建时的 terminal reservation，provider/tool/approval/delegation prerequisite evidence 只能消费各自副作用前建立的 operation reservation。容量不足的 operation MUST 在外部副作用前以稳定 `event.sequence_exhausted` fail closed，不消费 seq、不创建业务 evidence；未知结果保留 reservation并阻止 terminal。若既有状态违反容量不变量、high-water mark 与最大 seq 不一致，或 `seq=2147483647` 不是 terminal，任何新写入 MUST 以 `event.sequence_state_invalid` 零变更拒绝并要求人工处置，不得覆盖或删除 evidence。

#### Scenario: 容量不足在副作用前拒绝
- **WHEN** 新 operation 的最大 prerequisite event 预约会让 highest persisted seq、outstanding 与 terminal reservation 总和越过 `2147483647`
- **THEN** reservation 以 `event.sequence_exhausted` 零业务副作用失败，既有 operation 仍可消费自己的预约并在全部 prerequisite evidence 完成后由 terminal reservation 收口；SQLite/local 与 PostgreSQL 结果相同

#### Scenario: 稀疏高序号保留 terminal 容量
- **WHEN** run 已持久化 `seq={1, 2147483646}`，随后请求新的 operation reservation
- **THEN** 系统按 high-water mark `2147483646` 在副作用前拒绝 operation，terminal 仍可消费自己的最后一个预约；不得按两条 row 误判可用容量

#### Scenario: 非法最大序号状态拒绝继续写入
- **WHEN** 历史或直接数据库写入留下 `seq=2147483647` 的 non-terminal evidence
- **THEN** EventBus/sink 以 `event.sequence_state_invalid` 拒绝 terminal 和 non-terminal 新写入，既有 evidence、run 状态与序号均不改变

#### Scenario: 并发边界不产生部分写入
- **WHEN** PostgreSQL worker 或 local async caller 在序号预留边界并发发布 event
- **THEN** run 级锁/事务只允许容量不变量内的 reservation/event 提交，所有被拒绝 operation 都不消费 seq、不产生外部副作用、不留下未闭合 reservation

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
