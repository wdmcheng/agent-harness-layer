## ADDED Requirements

### Requirement: Run-scoped CanonicalEvent 必须携带 canonical trace
EventBus SHALL 要求所有具有 `run_id` 的 lifecycle、approval、tool、model、retrieval、eval 和 terminal CanonicalEvent 携带该 run 的 canonical `trace_id`。Event sink MUST 拒绝同一 run 中缺失或与 persisted run context 不一致的 trace；非 run telemetry 不受此要求影响。

#### Scenario: 同一 run 的事件 trace 一致
- **WHEN** run 依次产生 queued、started、approval、tool/model 与 terminal event
- **THEN** 所有事件保留各自唯一 event_id/seq，同时共享同一 canonical trace

#### Scenario: 错误 trace 被拒绝
- **WHEN** 下游组件尝试发布带空 trace 或不同 trace 的 run-scoped event
- **THEN** EventBus 在持久化和 provider fan-out 前拒绝该事件并产生封闭诊断，不改写 canonical trace
