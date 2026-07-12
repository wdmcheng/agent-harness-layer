## Context

当前 RUN-001 只在调用方提供 `X-Trace-Id` 时把 trace 写入 run execution context；CLI、测试和部分内部入口默认传入 null。CanonicalEvent、ApprovalRecord、tool/eval DTO 与多个存储列也允许 null。Product Spec 却要求 approval 必须关联 trace，新的 ModelUsageEvidence 也把 `trace_id` 定义为必填。这个差异横跨 API、runtime、worker、approval、event 和 migration，必须先建立单一 canonical trace 所有权。

## Goals / Non-Goals

**Goals:**

- 每个新 root run 在任何持久化业务事件、queue enqueue、tool/model/provider 副作用前取得唯一 canonical `trace_id`。
- local、service worker、checkpoint/resume、approval/audit 与后续 child run 复用同一 trace，不由各层重新生成。
- 让 ApprovalRecord 和 run-scoped CanonicalEvent 的 trace 关联可由数据库与合同测试验证。
- 对历史 nullable 数据执行确定性、幂等 backfill，并保留可审计迁移证据。

**Non-Goals:**

- 不接入 OTel exporter、SaaS provider、采样、baggage 或 trace 查询 API。
- 不改变 approval 状态机、model usage DTO、delegation 执行或 SSE transport。
- 不要求 request 与 trace 一一对应；同一 run 的恢复、审批和 worker 请求可以有不同 request_id。

## Decisions

1. **runtime composition 是 canonical trace 唯一所有者。** RUN-001、CLI 和内部 run create 都把可选 caller trace 交给同一 normalizer；合法且未冲突的值被保留，缺失时生成全局唯一 ID。任何下游服务只能读取已绑定值，不得自行补一个不同 trace。替代方案是在 EventBus 或 provider adapter 首次需要时生成；拒绝，因为 approval、queue 与首个 lifecycle event 可能先发生。
2. **caller trace 受控且不可重绑定。** 非空 caller value 必须满足稳定长度/字符合同；已绑定到另一 root run 时返回结构化 conflict 且零业务副作用。同一 idempotent run 仅在 caller trace 缺失或与首次 canonical trace 相同时安全重放；后续请求携带不同 trace 时返回 `409 trace.idempotency_conflict`，不改写首次绑定且不产生业务副作用。
3. **execution context 是跨进程传播源。** canonical trace 与 identity/request 一起写入私有 run execution context；queue message 继续只携带稳定 ref，worker 从持久化上下文恢复。checkpoint/resume、approval、tool/model 和 event service 都从该上下文继承。
4. **公开 DTO 只收紧既有字段。** ApprovalRecord 的既有 `trace_id` 从 nullable 改为 required，不增加第二字段。RunCreateResponse 暂不扩张；调用方通过自己的 header 或 RUN-003 events 获取关联。ModelUsageEvidence 直接消费 canonical trace，不承担生成所有权。
5. **历史 backfill 以 run 为单位确定生成。** migration 为每个缺失 trace 的历史 run 生成基于稳定 namespace 与 run id 的 UUIDv5 风格值，并把同一值写入该 run 的私有 execution context、approval、run-scoped event/audit/trace 记录。重复执行得到同一结果；已有非空值不覆盖。同一 run 已存在多个不同非空 trace、或记录无法唯一归属 run 时，migration MUST 在单事务内 fail closed，输出脱敏 run/record 标识并拒绝部分 backfill，不猜测 canonical 值。
6. **迁移完成后禁止新 null。** 数据库对 approvals 的 trace 列收紧非空；其他可承载非 run 事件的通用表保留 nullable schema，但 repository 对 run-scoped 写入强制非空。这样不把非 run telemetry 伪装为 run trace，又能对 Product P0 路径形成硬门禁。

## Affected Surfaces

- runtime run create、execution context、checkpoint/resume 与 queue/worker recovery。
- approval service/repository/API/CLI、CanonicalEvent/EventBus、audit/trace refs。
- RUN-001 可选 `X-Trace-Id` 校验和统一错误映射。
- Alembic migration、SQLite/PostgreSQL repository contracts 和 service smoke。
- 后续 `model-usage-evidence`、`agent-delegation-execution` 与 `sse-event-streaming` 的前置关联合同。

## Testing Seams

- API/CLI/内部入口缺失 trace 时生成、显式合法 trace 时保留、非法或冲突 trace 时零副作用失败。
- local 与 service queue/worker 读取同一 trace；restart、checkpoint、approve/deny/resume 后不改变。
- ApprovalRecord、audit 和所有 run-scoped CanonicalEvent 与 persisted run context 逐值一致。
- SQLite/PostgreSQL migration backfill 幂等；已有 trace 不改写；同一 run 的冲突非空 trace 与孤立数据都整批 fail closed。
- model usage 后续 change 只能消费 canonical trace，不能生成第二 trace。

## Risks / Trade-offs

- [Risk] 历史 JSON execution context 与多表 backfill 容易部分成功 → migration 在单事务内按 run 分组更新，失败整体回滚，并增加双数据库合同。
- [Risk] caller 重用 trace 可能打破全局唯一性 → 建立唯一绑定检查；同一 idempotent run 只在 trace 缺失或相同时复用原绑定，不同 trace 以 `409 trace.idempotency_conflict` 拒绝。
- [Risk] 强制 trace 会暴露此前未覆盖的内部入口 → 所有入口复用同一 normalizer，测试禁止直接构造 nullable ApprovalCreate。
- [Risk] 通用 event 表无法全列 NOT NULL → 只对 run-scoped repository 写入建立硬门禁，非 run telemetry 保留独立语义。

## Migration Plan

先增加 trace normalizer 与失败合同，再增加数据 migration/backfill 和 repository 非空门禁；随后切换 API/CLI/runtime/worker/approval/event composition，最后运行 local/service、restart/resume 与 model usage 前置合同。回滚 application code 前必须证明没有新格式记录依赖非空 trace；数据库 downgrade 只允许 disposable 环境或先恢复 nullable 约束，不能删除 backfill 值。完成后停在 `ready-to-archive`，不自动归档。

## Open Questions

无。外部 provider trace 映射与采样策略不属于本 change。
