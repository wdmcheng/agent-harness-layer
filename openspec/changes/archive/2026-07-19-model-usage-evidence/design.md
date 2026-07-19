## Context

当前 `ModelResponse` 只有自由形状 `token_usage` 与 `latency_ms`，embedding cache 只记录 provider latency；EventBus 已有 `model.request.started`/`model.usage.updated` 类型，TelemetryFacade 已提供 local-first、redaction 与 provider fan-out，但没有一份统一的 usage DTO 或强制关联路径。Phase 13.8 将依赖 durable child usage 做 parent aggregation，因此本 change 必须先让单次 model/embedding 调用产生不可由业务 agent 伪造的 provider-neutral evidence。

## Goals / Non-Goals

**Goals:**

- 统一 model 与 embedding 的 token、cost、latency、route/fallback/budget 字段和可用性语义，并逐字段遵守 API Contract 5.29。
- 在 adapter/router 组合边界注入 tenant/run/request/agent/trace 关联，并先写 local durable evidence。
- 让完成、受控拒绝和 provider 失败都留下脱敏、可结算证据；用 fake provider 提供稳定的 5 秒入口时延门禁。

**Non-Goals:**

- 不执行 delegation 或聚合 parent/child，不新增 SSE transport。
- 不持久化 prompt/embedding 原文、provider raw response/client，不依赖真实 provider 或 SaaS telemetry。

## Decisions

1. **实现现有 provider-neutral DTO，不扩张字段。** `ModelUsageEvidence` 由 adapter/router 边界构造，字段严格为 API Contract 5.29 的 `usage_kind`、tenant/provider/model、nullable input/output tokens、`cost_usd`、`cost_status`、`latency_ms`、decision、run/agent、可选 request 和必填 trace。生命周期 outcome/error/timestamp 使用稳定 event type 与有界 payload 表达；单次调用 `usage_call_id` 的唯一 CanonicalEvent 路径固定为 `payload.correlation.usage_call_id`，TelemetryFacade 映射后固定为 `TelemetryRecord.payload.correlation.usage_call_id`。两处都是非空 string，不新增 CanonicalEvent envelope 顶层字段，也不进入 `ModelUsageEvidence`。替代自由路径或新增第二套字段都会造成契约漂移，因此拒绝。
2. **缺失、零值与非法值分开表达。** token/latency 只接受非 bool 非负整数，cost 只接受非 bool、有限、非负 number；bool、负数、NaN/Infinity 在持久化和聚合前拒绝。token 不可用使用 null，不伪造 0；cost 只有 provider 报告或来自带版本/来源的可验证 price configuration 才能写入 `cost_usd`，分别标 `reported` 或 `estimated`，两者都要求非 null cost；否则必须为 null + `unavailable`。estimated 的安全来源落在既有 `decision.price_source_ref` 与 `decision.price_source_version`，不新增顶层 DTO 字段，也不内联完整价目。币种由字段名固定为 USD。
3. **started 与调用级最终 usage 共用事件关联。** 每次调用在 adapter 副作用前生成稳定 `usage_call_id`，并与 `run-trace-correlation` 提供的 canonical trace 一起发布 `model.request.started`；composition 必须以 durable tenant/run/request/agent/trace 关联和稳定的语义调用槽位生成该 ID，禁止 invocation seam 随机回退，也不得把 prompt、embedding input 或其他敏感业务输入放入 ID。完成、budget/policy 拒绝或 provider 失败后发布恰好一条调用级最终 `model.usage.updated`。该 event 只结束 `usage_call_id` 生命周期，`CanonicalEvent.terminal` MUST 为 false，不能关闭 run stream；run terminal marker 仍只属于 `run.completed` / `run.failed` / `run.cancelled`。Embedding 精确复用这两个 event type，以同一 DTO 的 `usage_kind=embedding` 区分，不新增“等价”事件名。`usage_call_id` 只存在于 `CanonicalEvent.payload.correlation.usage_call_id`，并由 TelemetryFacade 原样映射到 `TelemetryRecord.payload.correlation.usage_call_id`；不得使用 envelope 顶层字段、`payload.usage_call_id` 或 `payload.metadata.usage_call_id` 等替代路径。
   - Fallback 是真实执行路径，不是零副作用拒绝：router 先选定实际备用 provider/model，再为该实际调用发布 started，最终 usage 记录原 route/fallback decision 与实际 provider/model。只有 hard budget reject、policy intervention/rejection 等阻止 provider 调用的结果才写零 provider side effect 的调用级最终 usage；两者的 `model.usage.updated.terminal` 都固定为 false。
   - Embedding cache lookup 也是一次调用级生命周期：hit 仍发布 started/final，`latency_ms` 只记录本次 lookup 墙钟，token/cost 为 null + unavailable，decision 固定 `cache_status=hit`、`provider_called=false`；cache row 的首次 provider latency 只用于 cache evidence，不能复制成本次 usage latency，且不得产生 provider side effect。
4. **EventBus 是 canonical usage 的唯一 local durable 写入者。** 本 change 以 `MODIFIED Requirement` 缩窄既有 TelemetryFacade 主规格：ordinary provider-neutral record 仍由 Facade 写 local/jsonl；`model.request.started` / `model.usage.updated` 必须先由 EventBus 持久化，再把同一个 event 交给只做 provider fan-out 的 Facade。Facade 绝不为 usage 再调用 local sink 或创建 CanonicalEvent。外部 provider failure 通过有界 facade result 和独立、非 usage、幂等的 provider-status evidence 表达，不回滚、覆盖或重复 local usage。若 EventBus durable write 失败，不得调用 Facade，也不得把调用表述为已完整结算；durable settlement/outbox 保留待补投状态，恢复只重放 evidence dispatch，不重放 provider。
5. **失败 evidence 有界且可结算。** provider 异常映射到稳定 error code/outcome；保留 latency、已知 usage、route/budget decision 和关联字段，移除 prompt、embedding input、headers、secret、raw exception/response。Redaction 在 CanonicalEvent 与 telemetry 两个出口都执行，防止未来 sink 绕过。
6. **性能门禁测入口，不测随机微步骤。** `scripts/smoke_local.py` 使用 fake provider 从公开入口到唯一 terminal 记录 monotonic 总时延并断言小于 5 秒；fixture/数据固定，失败输出阶段时间，避免用单元测试墙钟偶然性替代验收。

7. **Durable settlement/outbox 拥有跨崩溃恢复。** 每次 started 调用在 provider 副作用前创建以 `(tenant_id, usage_call_id)` 唯一的 `run_evidence_outbox`/settlement 记录和稳定 usage event id；provider 结果、脱敏 usage 摘要或确定性失败只写入该记录一次，状态至少区分 `started`、`result_persisted`、`published`。sink 写入失败或确认丢失后，startup/runtime recovery 只从已持久化结果幂等补投同一 event id，绝不重新调用 provider。service worker 必须在 DBOS runtime 启动前恢复全部已有确定结果，并在 queued run 重放或执行前再次做 run-scoped recovery；恢复只处理 model/embedding operation kind，不能误消费 approval 等共享 outbox 项。未知结果保持 pending/needs_review并阻止 terminal，不以零值或伪造失败收口。
8. **Run terminal 是 public 且为有序 outbox 的最后一项。** runtime 收口前恢复或确定性封闭所有已开始的 usage 调用，使每条最终 usage 的 seq 小于 terminal seq；approval continuation 的确定性结果先把 `approval.resolved` 与 run terminal 写入同一 ordered outbox group，resolution 排在 terminal 前，二者 durable 后才公开 approval resolution。三种 run terminal event 必须显式写入 `visibility=public`；EventBus 与 local/PostgreSQL sink 在持久化前拒绝 non-public terminal，并在 terminal 后统一拒绝 terminal 和 non-terminal 业务事件。terminal 可见即证明所有 prerequisite evidence 已可见，恢复不得再补写前置 evidence或重放 provider/tool。
9. **Outbox 在副作用前预约 event 容量。** `0014` 为每个 run 持久化一个 terminal reservation；中央 typed registry 以版本化、封闭 `operation_kind` 派生 provider/tool/approval/delegation operation 的最大 prerequisite event 数，业务 agent/HTTP payload 不能提供或缩小该值。Repository 在外部副作用前通过 run row lock 或等价 CAS 原子增加 outstanding reservation。容量基数使用 `highest_persisted_seq`，不使用会忽略 seq 空洞的 row count；`highest_persisted_seq + outstanding + terminal` 不得超过 `2147483647`。预约消费、event 插入和 high-water mark 推进处于同一锁/事务，不足时以 `event.sequence_exhausted` 零业务副作用拒绝。实际 evidence 持久化后按实耗结算，确定不产生的余额才释放；结果未知时预约保持占用并与既有 settlement 一起阻止 terminal。替代方案是只在 publish 时检查最后一个 seq；拒绝，因为 usage/resolution 可能已产生但没有容量写入。
10. **Canonical envelope 有唯一字节表示和持久化硬上限。** 公共 `canonical_event_bytes()` 对 `CanonicalEvent.to_payload()` 使用 UTF-8、非 ASCII 转义关闭、排序键、紧凑分隔符并拒绝 NaN；JSONL 换行与 SSE frame 开销不计入 envelope bytes。正常写入的完整 envelope 最多 `65536` bytes；EventBus 先把大 payload artifact 化并重算，仍超限时以 `event.envelope_too_large` 在持久化和 fan-out 前拒绝。local/DB sink、legacy 校验和 SSE page 必须复用同一 serializer。历史或 direct-write 超限 row 以 `event.envelope_state_invalid` fail closed，不截断、不伪造可交付 evidence。

## Affected Surfaces

- `agent_harness.models`/`embeddings` 的 DTO、provider/router seam 和 adapters。
- CanonicalEvent payload contract、公共 `events/serialization.py::canonical_event_bytes()`、EventBus sinks、TelemetryFacade 与 service-app runtime composition。
- local JSONL、现有 PostgreSQL canonical event storage，以及 `0014` 新增的 durable evidence outbox/usage settlement和 per-run event capacity reservation；approval continuation 的 resolution/terminal 也复用该有序恢复边界。
- contract/integration/eval/smoke 证据；不新增 HTTP endpoint。

## Testing Seams

- fake model、Pydantic AI adapter 替身与 OpenAI-compatible embedding 替身输出同一 DTO。
- reported/estimated/unavailable cost、token unavailable/真实零值、bool/负数/NaN/Infinity 与 cost-status 组合拒绝、fallback、budget/policy required、timeout/provider failure。
- embedding cache hit 的 started/final evidence、本次 lookup latency、null token/cost、`cache_status=hit/provider_called=false`、首次 provider latency 不复用与零 provider side effect。
- 同一次调用的 started/最终 usage evidence 逐值核对 tenant/run/request/agent/trace；最终 usage 恰好一条、`CanonicalEvent.terminal=false` 且 seq 小于 run terminal；覆盖未结算调用阻止 terminal 和 terminal 后写入被拒绝。
- local sink 先于 provider fan-out；provider failure 后仍可读取 local evidence；sink 写前失败、写后确认丢失和进程重启都只补投同一 event id，不重放 provider。
- approval completed/确定性 failed/deny 的 resolution evidence 先于唯一 public terminal，二者恢复使用稳定 event id 且不重放 tool handler；local/PostgreSQL sink 均拒绝 non-public terminal。
- run 创建时 terminal reservation、provider/tool/approval/delegation 副作用前的最大 event 预约、稀疏高 seq/high-water mark、并发容量竞争、实际结算/确定释放、未知结果保持预约、容量不足零副作用，以及 SQLite/PostgreSQL 相同结果。
- 公共 serializer 的 UTF-8/键顺序/Unicode/NaN 与精确边界、`65536` bytes 正常 envelope 上限、payload artifact 化后重算、仍超限写前拒绝，以及 legacy/direct-write 超限 row fail closed。
- secret/prompt/embedding/raw response fixtures 不进入 DTO、event、trace、error 或 provider payload。
- public fake run smoke 在 5 秒内完成并输出可重复定位的阶段时延。

## Risks / Trade-offs

- [Risk] provider token 命名和 cost 数据差异大 → adapter 只映射可证明字段，未知值显式 unavailable，不猜测。
- [Risk] local write failure 与 provider 已产生调用成本之间出现不确定性 → provider 副作用前建立 durable settlement，结果只持久化一次；失败与确认丢失只重放稳定 outbox event，不自动重试非幂等 provider 调用。
- [Risk] 每次调用增加 event 数量 → payload 保持有界摘要，大内容用 ref；P0 接受可审计性带来的固定开销。
- [Risk] 5 秒阈值受机器负载影响 → fake/local fixture 无网络依赖，记录分阶段时延并允许 CI 基于明确环境重跑，但不得放宽产品门禁。

## Migration Plan

先引入 DTO 与 red contract tests，再以 `0013a_run_trace_event_hardening` 为直接前置增加 Alembic revision `0014`，实现 `run_evidence_outbox`/usage settlement、event capacity reservation及其唯一键、顺序和恢复状态。Migration 必须在 writers/worker 已停的窗口预检旧 run：terminal run 不建预约；非 terminal run 建一个 terminal reservation；只允许能从持久化 checkpoint/approval/tool 状态映射到封闭 operation kind 的活跃状态，并按 registry 回填 outstanding reservation，未知或矛盾状态在 DDL/UPDATE 前整批拒绝。随后让 fake/model/embedding adapters 产出 evidence，接入 EventBus/TelemetryFacade、approval continuation 与 service runtime，最后启用 local latency smoke。`0014` downgrade 同时要求 outbox/settlement/capacity evidence 全空和 Alembic x 参数精确为 `allow_empty_evidence_downgrade=true`；缺失/重复/非法 opt-in 或存在 evidence 都在 DDL 前拒绝并保留兼容读取。旧 `ModelResponse.token_usage` 可在内部适配期读取，但公共 evidence 必须由统一 DTO 生成；迁移完成后禁止业务 agent 拼 raw usage。本 change 不自动归档。

## Open Questions

无。Delegation aggregation 与 SSE 已由后续相关 change 承担，并继续受 DEV-PLAN 串行 DAG 与联合审查约束。
