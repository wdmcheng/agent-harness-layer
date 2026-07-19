## ADDED Requirements

### Requirement: Run lifecycle 传播持久化 canonical trace
RunOrchestrator SHALL 在创建 run 时取得 canonical `trace_id` 并写入私有 execution context。checkpoint、resume token state、queue/worker recovery 和 terminal transition MUST 读取该持久化值，不得接受下游调用方以参数覆盖或在缺失时静默生成另一值。

#### Scenario: Idempotent replay 保留首次 trace
- **WHEN** 同一 idempotency key 重放已创建的 run，且 caller trace 缺失或与首次 canonical trace 相同
- **THEN** 系统复用首次 run 与其 canonical trace，不改写 execution context 或产生重复 lifecycle event

#### Scenario: Idempotent replay 拒绝不同 trace
- **WHEN** 同一 idempotency key 重放已创建的 run，但 caller trace 与首次 canonical trace 不同
- **THEN** 系统返回 `409 trace.idempotency_conflict`，不改写 execution context，且不产生新 run、event、queue message、approval 或 provider side effect

#### Scenario: Resume 使用原 trace
- **WHEN** run 从 checkpoint 恢复并产生新的 resume request_id
- **THEN** resumed 与 terminal event 使用原 canonical trace，同时保留新的 request_id 作为本次入口关联
