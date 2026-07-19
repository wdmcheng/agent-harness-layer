## ADDED Requirements

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
