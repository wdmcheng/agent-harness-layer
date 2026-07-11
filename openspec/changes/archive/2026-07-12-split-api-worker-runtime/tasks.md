## 1. Queued Run Runtime Seam

- [x] 1.1 先以 migration/repository合同锁定 0012 run operation/effective-key/首次-request/enqueue/message/context/owner/workflow refs，approval operation/首次-request/reviewer/decision/request-hash、`resolution_state=claimed|execution_owned`、enqueue/message/workflow私有列与DTO，以及 claimed->execution_owned CAS、过期无claim由matching APR-002审计旧refs、换新lease-id/operation、原子刷新该operation首次request correlation/重绑fingerprint并清空active refs的 takeover，event envelope/terminal；再实现 `created+enqueue_pending -> queued+run.queued -> execute`和 local inline。
- [x] 1.2 以非默认 roles/permissions/auth_method、source/trust/context/request/trace fixtures锁定脱敏身份快照与跨进程重建，再覆盖 created -> running -> completed/failed/waiting、guardrail deny/require-approval和重启后唯一终态。
- [x] 1.3 先以 PostgreSQL/SQLite migration和 EventSink合同锁定完整 CanonicalEvent envelope、run-row串行 seq、event id幂等、每 run唯一 terminal、legacy fallback与受限 downgrade，再实现 `PostgreSQLEventSink`和 EventBus persisted-event seam。

## 2. DBOS 受控 Adapter

- [x] 2.1 核验并锁定 DBOS 2.26.0，先以 adapter/fake合同锁定 tenant/operation workflow、execute/approval durable steps、stable singleton executor id、同 operation重放、approval lease fencing、状态映射和 vendor边界，再实现 handler lifecycle/config gate。
- [x] 2.2 以真实 PostgreSQL进程合同证明 A使用 `agent-harness-service-worker`建立 PENDING workflow后 hard-exit，B在 A完全退出后复用同 executor id恢复同 workflow/durable step；并验证两个 active同-id worker fail closed、initial waiting后 approval独立 workflow、operation幂等和 owner refs一致。

## 3. Service API 与 Worker

- [x] 3.1 先更新 API-Contract的 RUN-001：service queued成功明确返回202、local inline继续返回200、queued成功时点、503 `run.enqueue_unavailable`、同 key API retry、无 key非幂等但 worker recovery不 orphan、私有 enqueue字段不进DTO。合同覆盖 Redis成功/DB更新/event失败窗口、startup/pickup对账、稳定 `run.queued`和 executor为零，再注入 queue/mode。
- [x] 3.2 先逐字段更新 API-Contract的 APR-002 service 202 waiting/queued、503 `approval.enqueue_unavailable`、仅 `resolution_state=claimed`、`enqueue_pending|queued`且无 tool claim并满足本次 reviewer/decision/规范化 request hash与私有 fingerprint完整一致的 active-lease API retry窄例外、私有 enqueue字段不进 ApprovalStatus/DTO、其他 active lease仍409；合同覆盖 deny零 lease/queue/handler、并发仲裁、同事务 enqueue pending、API retry、仅 active `claimed+enqueue_pending`且持久 fingerprint完整并无 claim的 worker recovery、pickup CAS execution_owned、过期无claim由matching APR-002换新lease-id/operation并为新operation固化本次request id/重绑fingerprint、旧 lease/fencing和唯一证据，再实现分支。
- [x] 3.3 先以 worker seam锁定 startup pending run/approval requeue、pickup queued/evidence对账、DBOS execute/resume、fenced ack、不确定不 ack和reclaim，再实现 loop/`--once`/关闭。

## 4. 集成与回归证据

- [x] 4.1 运行 execution-context/migration/PostgreSQL event sink、queued runtime、RUN-001/APR-002、worker、DBOS PostgreSQL、checkpoint/approval、trust/audit correlation、OpenAPI和 local CLI定向回归，证明 service split不破坏 local inline。
- [x] 4.2 更新本 change tasks、严格校验和受影响契约证据，确保下游 Compose change 只通过公开 queue/runtime seam 组装。
