# Phase 13 OpenSpec 关系矩阵

## 依赖 DAG

```text
durable-run-queue
  -> split-api-worker-runtime
    -> service-profile-deployment-proof
```

三个 change 共享 Phase 13 的 run queue、runtime、service-app 和 smoke 验收，属于关联变更。任何实现开始前，必须完成每个 change 的严格校验与独立变更契约审查，并由新的 code-reviewer 对全部 artifacts 和上游真相源完成联合 Stage 1/2 PASS。

## 关系与所有权

| Change | 直接依赖 | 主要公共 seam | 主要文件所有权 | 共享验收 / 冲突控制 |
|---|---|---|---|---|
| `durable-run-queue` | 无 | `RunQueueMessage`、delivery receipt、`RunQueue` protocol、Redis Streams adapter | `runtime/queue.py`、`adapters/queue/redis.py` 及 queue contract tests | 只负责交付、ack/reclaim 和消息幂等，不执行 run、不修改 HTTP |
| `split-api-worker-runtime` | `durable-run-queue` | created/run.queued submit、execute/approval resume、DBOS adapter、PostgreSQL event sink、service RUN-001/APR-002、worker loop | orchestrator/repository、0012 migration、DBOS/event adapter、runtime composition、runs/approval route、runtime worker | 消费 queue 公共 seam；保持 local inline；不编排 Compose 或扩展 Phase 14 文档 |
| `service-profile-deployment-proof` | 前两个 change | 四服务 Compose、HTTP-to-worker smoke、部署边界与 ADR | Compose/Dockerfile、Makefile、smoke 脚本、README/API contract/architecture/ADR/DEV-PLAN | 不回改 queue/runtime 语义；只通过已审查公开 seam 组装和证明 |

共享 export、依赖清单、lockfile、模板 contract tests 和 Phase 状态文档按 DAG 顺序修改。任何共享接口、验收或文件所有权变化都会使受影响 change 的契约审查与联合审查失效，必须从 Stage 1 重跑。

## 共享验收

- API 创建的 run 必须由独立 worker 执行同一 `run_id`，并在共享 PostgreSQL 与 event sink 中产生可读取终态证据。
- queue message 必须保留 `request_id`、`idempotency_key`、`tenant_id`、`run_id`；重复提交、重复投递和 pending reclaim 不得产生第二个 run 或第二个 terminal side effect。
- Queue与 DBOS幂等按 tenant/operation作用：initial execute和每个 approval lease拥有不同 operation id；同 operation重试复用，跨 operation不冲突。initial workflow ref存 run，approval workflow ref存 resolution lease。
- P0 Compose仅允许一个 active service worker；worker进程使用稳定 DBOS executor id，替代进程在前任完全退出后复用该 id恢复 PENDING workflows。多 worker pool必须引入 Conductor或新契约，不能共享 executor id并发运行。
- 原 API actor snapshot、approval lease/grant、source/trust/context、guardrail/audit与 request/trace correlation必须从 PostgreSQL权威状态跨进程恢复；profile default和queue payload不得替代。
- APR-002只有 approve进入 operation queue；deny在 API/repository原子收口。approve lease/operation的 enqueue状态与 `resolution_state=claimed|execution_owned`必须持久化；API retry仅在 `claimed`、`enqueue_pending|queued`、尚无 tool claim且本次 reviewer/decision/规范化 request hash与私有 fingerprint全部相同时复用同 lease，其中 queued不得重投。worker startup recovery只补投 active `claimed+enqueue_pending`、无 tool claim且已保存完整 fingerprint的 lease；worker pickup先 CAS为 `execution_owned`并保存 DBOS owner/ref。过期 execution-owned且无 claim时仅由 fingerprint匹配的真实 APR-002在同一事务审计旧 refs、换发新的 resolution lease id、按新 id派生 operation、以本次 request id建立新 operation首次 correlation、重新绑定已验证 fingerprint并清空 active message/workflow refs，旧 operation fail closed。
- RUN-001与 approve均先持久化私有 operation/fingerprint/`enqueue_pending`；Redis接受并对账 queued/message ref后才发布稳定 queued evidence。RUN-001同客户端 key重试可补投；approve API retry、worker startup与 execution-owned takeover分别遵守前述互斥边界，pickup reconciliation不得绕过 fingerprint、claim、resolution或 enqueue state检查，私有字段不进入 public DTO。
- API/worker 拆分后继续保留 `source_ref`、`trust_level`、context assembly trace、guardrail/audit 与适用 correlation fields。
- `make smoke-service` 必须使用真实 PostgreSQL、Redis、独立 API 和 worker，不能以 local/in-memory/mock 证据替代。
- service smoke必须分别证明 DBOS owner/workflow durable state建立后的 hard-crash recovery，以及 `examples.dev_assistant` application checkpoint -> approve worker continuation / deny零 continuation；DBOS metadata或日志不能冒充 checkpoint。
- local profile、既有 checkpoint/resume、approval continuation、四示例 eval 和 OpenAPI 契约不得回归。

## 允许实现顺序

1. `durable-run-queue` 建立 provider-neutral queue 与真实 Redis delivery/reclaim 基础。
2. `split-api-worker-runtime` 在稳定 queue seam 上拆分 service RUN-001 与 worker execution，并接入 DBOS workflow idempotency。
3. `service-profile-deployment-proof` 通过前两者公开 seam 组装四服务 Compose、真实 smoke 与部署文档。

不允许跳过上游 change，也不允许把 Phase 14 深度文档或 Phase 15 release automation 塞入任一 change。
