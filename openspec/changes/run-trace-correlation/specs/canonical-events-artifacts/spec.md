## ADDED Requirements

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
