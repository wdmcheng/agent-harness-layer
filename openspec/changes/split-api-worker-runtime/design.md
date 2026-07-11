## Context

当前 `start_run` 同时创建并执行，service RUN-001 无法把任务交给独立 worker；`runtime_worker --once` 只是自行创建另一条 run。Phase 13 的 queue change 提供 fenced Redis delivery，本变更负责拆 submit/execute/approval-resume seam，并用 DBOS 2.26.0 durable workflow与 PostgreSQL application state 抵御重启。现有 `canonical_events` 表尚未成为 EventSink，JSONL只有进程内锁，因此本变更同时固定 PostgreSQL event sink与 0012 migration。

## Goals / Non-Goals

**Goals:**
- service RUN-001 只创建/排队，worker 执行同一持久化 run。
- local/CLI inline 行为不变。
- DBOS只在 adapter边界，按 tenant/operation生成 workflow id；initial execute与每个 approval lease独立。
- 重复 pickup、reclaim 与进程中断不重复 run 或 terminal side effect。
- 原认证 actor、approval lease/grant、trust/context/audit correlation 与完整 event envelope可跨进程恢复。

**Non-Goals:**
- 不让 DBOS 替代应用 PostgreSQL run/checkpoint/event 真相源。
- 不编排 Compose、不拆其他 gateway、不新增 endpoint。

## Decisions

1. **把 orchestrator 拆为 `submit_run`、`execute_run`，`start_run` 组合两者。** local/CLI复用原行为，service API只调用 submit；route不得复制 repository逻辑。
2. **`created` + 私有 enqueue state 区分已创建与已入队。** 0012为 runs增加 execution context、operation/effective key/首次 request id、`enqueue_pending|queued`、message/owner/workflow refs；approvals增加 operation/首次 request/reviewer/decision/规范化 request hash、`resolution_state=claimed|execution_owned`、enqueue/message/workflow refs；events增加 envelope/terminal约束。`run.queued`只在 Redis接受并对账 queued后发布。
3. **queue只传 refs，worker从 storage重建。** tenant必须与 run/context匹配，identity包含原 user/session/roles/permissions/auth_method；profile default不能替代。run input中既有 source/trust refs、context assembly记录和 audit correlation继续从 PostgreSQL读取。
4. **Redis负责 pickup，DBOS按 operation负责 durable workflow envelope。** adapter注册 execute/resume两类 async workflow与 durable step，workflow id使用 tenant/operation。P0 Compose是 singleton worker，显式稳定 `executor_id=agent-harness-service-worker`；A完全退出后 B复用该 id，DBOS自动恢复归属 PENDING workflow。两个 active进程不得共享 id，未来多 worker需要 Conductor/独立设计。initial owner/ref写 run，approval ref写 resolution；handler重入先读权威状态。
5. **DBOS状态分类决定 ack。** system DB不可用、取消、handle结果不确定与可恢复状态不 ack；确定 `ERROR`/`MAX_RECOVERY_ATTEMPTS_EXCEEDED` 先映射脱敏 failed/audit/terminal，成功持久化后 ack。执行 step重放先读 run，避免 terminal后再调 executor。
6. **run与 approve都有可达补投。** RUN-001/approve分别在 DB事务写 operation、immutable correlation/fingerprint与 enqueue_pending；enqueue成功写 queued/message，稳定 event后才返回成功。RUN-001同客户端 key重试可补投；approve API retry仅在 `resolution_state=claimed`、enqueue state为 `enqueue_pending|queued`、尚无 tool claim且本次 reviewer/decision/规范化 request hash与私有 fingerprint全部相同时复用同 lease/operation，其中 queued不重投。worker startup只补投 active `claimed+enqueue_pending`、无 tool claim且持久 fingerprint完整的 approval；pickup先 CAS为 `execution_owned`并保存 workflow owner/ref。过期 execution-owned且无 claim的 takeover只由 fingerprint匹配的真实 APR-002触发：审计旧 refs后换发新 resolution lease id、按该 id派生新 operation，以本次 request id建立新 operation首次 correlation、重新绑定已验证 fingerprint并清空 active message/workflow refs。approval fingerprint/state用私有列，不放 public metadata。deny零 queue。
7. **service event固定 PostgreSQL sink。** 0012为 `canonical_events` 增加完整 `envelope_json` 并建立每 run唯一 terminal索引；sink通过锁定 application run row串行分配 seq，event id重复返回已有 envelope。EventSink写 seam返回实际 persisted event；LocalJsonl适配同一协议但仅用于 local。

## Affected Surfaces

- runtime orchestrator、run repository compare-and-set/execution-context seam、approval repository enqueue-state/recovery seam、approval service approve queue/deny atomic分支、runtime exports。
- DBOS adapter 从占位 protocol 升级为可配置真实实现；DBOS 2.26.0 pin。
- PostgreSQL EventSink、`canonical_events.envelope_json`/terminal index 与 `agent_runs` execution columns 的 0012 migration、SQLite/PostgreSQL repository parity。
- service runtime composition增加 queue/runtime mode，RUN-001/APR-002 service分支与 worker loop。
- API contract 的 service RUN-001 `created`、APR-002 queued/in-progress状态语义和定向 OpenAPI contract tests。

## Testing Seams

- Orchestrator submit/execute：同一 run、并发 claim、terminal replay、checkpoint/waiting。
- RUN-001：service 模式 worker 停止时 queued，local 模式 inline。
- Worker：fake queue/DBOS adapter下 execute/resume approval、startup pending-enqueue recovery、ack、不确定失败不 ack、reclaim幂等和 stale lease拒绝。
- PostgreSQL EventSink：跨实例 seq、event idempotency、terminal竞态、完整 envelope round-trip。
- 真实 DBOS PostgreSQL条件测试：stable executor id、A hard-exit后 B复用同 id恢复 PENDING workflow/durable step、同 workflow重放、running同 owner恢复、并行同-id fail closed和状态分类。
- 静态 vendor/import 与 DTO serialization 边界。

## Risks / Trade-offs

- [DBOS async handler 注册依赖进程内 composition] → worker启动时先构造 execution/resume handler再 launch DBOS；durable workflow参数只保存 refs，重启后重新注册同名 handler。
- [API已持久化但 Redis enqueue失败] → run/approve都保存 enqueue_pending；RUN-001同客户端 key的 API retry可复用原 operation。approve API retry仅在 `resolution_state=claimed`、`enqueue_pending|queued`、无 tool claim且本次 reviewer/decision/规范化 request hash与私有 fingerprint全部相同时复用原 lease/operation；worker startup只恢复持久 fingerprint完整、无 claim的 active `claimed+enqueue_pending` approval，pickup reconciliation必须先转为 `execution_owned`。过期 execution-owned takeover只由 matching APR-002触发，并以该请求刷新新 lease/operation的首次 correlation与已验证 fingerprint。无客户端 key的原 run由 recovery补投；新 HTTP请求仍按非幂等语义可创建新 run。
- [worker claim 后长期 running] → application run保存 initial execute owner/ref；只有同 execute operation可重入。approval owner/ref保存在 resolution lease，旧 lease拒绝并留审计。
- [DBOS 与应用表双重状态] → 应用 run/checkpoint/event 始终为公开真相源，DBOS 只提供执行恢复证据和 workflow ref。

## Migration Plan

先部署 queue contract，应用 0012 migration与兼容读取，再升级 event/runtime/worker，最后切 service RUN-001/APR-002到 enqueue模式。0012对旧 run/event保留 nullable/fallback读取；有未完成 service run、resolution lease或新 envelope evidence时禁止 destructive downgrade，生产采用 forward fix。local profile行为不变。

## Open Questions

无；P0明确 singleton stable executor recovery。多 worker并发、DBOS Conductor和跨语言 portable workflow不属于 P0。
