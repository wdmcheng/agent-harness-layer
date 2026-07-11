## Context

现有 Compose 只有 PostgreSQL/Redis，`smoke_service.py` 在宿主进程直接调用 worker `--once`，无法证明 HTTP API 与独立 worker 协作。上游两个 change 提供 Redis queue 与 split runtime，本变更只通过公开 seam 组装真实部署证明。部署边界图已存在但仍标记 Phase 13 待实现，仓库尚无 `docs/adr/`。

## Goals / Non-Goals

**Goals:**
- Compose 管理 PostgreSQL、Redis、API、worker 四服务与共享配置。
- smoke 从真实 HTTP 创建 run，再从 HTTP detail/events 证明 worker 执行。
- 证明重复提交/重投幂等，并产出脱敏可审计证据。
- 同步架构图、ADR、README、API contract 和 DEV-PLAN 状态。

**Non-Goals:**
- 不提供生产 orchestrator、Kubernetes、secret manager、autoscaling 或其他物理微服务。
- 不扩展 Phase 14/15。

## Decisions

1. **模板增加单一 Dockerfile，Compose 用不同 command 启动 API/worker。** 两者安装同一 service-app wheel/core source并共享 profile，避免维护两套镜像。备选宿主 API + 容器 worker 不能证明复制模板部署边界。
2. **容器内部使用服务名 DSN，宿主 smoke 只通过映射 API 端口。** profile 允许环境变量覆盖 storage/queue/events/DBOS URL；默认本地文件仍服务宿主命令。备选把 localhost DSN 写死会在容器内连接失败。
3. **迁移作为显式一次性 Compose service/命令先行。** API/worker不并发执行 Alembic；smoke先等待依赖、运行到包含 execution context/PostgreSQL event sink的 head，再启动应用进程。
4. **smoke使用真实 service credential。** 一次性 bootstrap通过公开 ApiKey repository在隔离 PostgreSQL写 token hash/actor permissions；明文只存在 smoke进程内存/环境，RUN-001缺失/无效 token必须 401且 run/queue零副作用，有效 token证明原 actor snapshot进入 worker。禁用 verifier或使用 local default不算通过。
5. **smoke在 DBOS owner/durable state建立后 hard crash。** P0 worker A使用稳定 `executor_id=agent-harness-service-worker`，持久化 owner/workflow并进入 PENDING/durable状态后 hard-exit。smoke确认 A容器完全停止，再让 B复用同 executor id；DBOS恢复归属 workflow，B同时 `XAUTOCLAIM`同 entry。逐值对比 executor/workflow/owner/status；不得在 owner前退出、并行复用 id或用 HTTP/XADD重投替代。
6. **shared checkpoint用真实 dev_assistant approval链证明。** 第二条 run触发确定性 executor-produced approval：worker写 application checkpoint/waiting/approval后 ack initial operation；API读取 evidence并分别验证 approve queue/worker resume 与 deny零 continuation。DBOS metadata不能替代 checkpoints表与 CanonicalEvent。
7. **event stream固定 PostgreSQL sink。** 上游 runtime change提供数据库级 seq/terminal约束和完整 envelope；Compose不挂共享 JSONL。API detail/events只读 PostgreSQL sink，smoke逐项核对 queued/started/checkpoint/approval/terminal及 correlation。
8. **默认全清，显式选项才保留 PostgreSQL volume。** finally始终删除本轮 container/network/queue namespace/credential/env/temp；默认 `down -v`。`SERVICE_APP_KEEP_DATA=1`仅保留命名 volume并输出精确 cleanup命令，不能含真实 token。
9. **架构产物同步三件套。** `.drawio`是首要源，`.excalidraw`与 PNG语义同步；ADR只固定 P0选择和拆分顺序，不提前写 Phase 14全量维护指南。

## Affected Surfaces

- 模板 Dockerfile、Compose、`.dockerignore`、Makefile、service profile/env example、smoke 脚本。
- 模板复制 smoke 与 Compose contract tests。
- 根/模板 README、API-Contract、DEV-PLAN、部署架构图三件套、`docs/adr/0001-p0-service-boundaries.md`。
- 无新 HTTP endpoint；RUN-001 的 service 状态语义和 health/readiness 说明更新。

## Testing Seams

- Compose config 静态解析：四服务、依赖、commands、healthchecks、共享 env。
- workspace 外 wheel-only template smoke。
- 真实认证 HTTP RUN-001 -> Redis四字段 -> DBOS owner/durable state -> worker A hard crash -> worker B reclaim/同 workflow恢复 -> PostgreSQL terminal/events。
- `examples.dev_assistant` waiting checkpoint -> APR-002 approve enqueue/worker resume，以及独立 deny零 queue/handler；approval enqueue failure补投。
- 缺失/无效 credential零副作用、脱敏失败日志、compose project/queue namespace隔离、默认全清与显式 volume保留。
- 架构/ADR/README 契约与 OpenAPI 无漂移。

## Risks / Trade-offs

- [构建上下文拿不到核心 wheel] → 根验证先 build wheel并通过显式 build arg/context复制；workspace 外 smoke 使用已提供 wheel，不回连仓库源码。
- [API key profile阻塞 smoke] → 隔离 bootstrap写 token hash，明文仅在本轮进程环境；禁用 verifier/dev default禁止作为证据。
- [worker failpoint污染生产] → 只在显式 smoke环境于 owner/durable state后 hard-exit，不进入默认 runtime；第二 worker仅在前任容器确认停止后复用 stable executor id。
- [PostgreSQL event sink迁移失败] → migration先行且 API/worker readiness依赖 head；旧 envelope fallback与受限 downgrade由上游 change验证。
- [Compose验证时间和残留资源] → 唯一 project/namespace、`--wait`、超时和 finally cleanup；不执行全局 Docker清理。

## Migration Plan

先构建新镜像、生成本轮 credential并运行 migration，再启动 API/worker；旧 local profile不受影响。回滚前停止 producer、审计未完成 queue/DBOS workflow；有新 execution/event/approval evidence时生产只 forward fix。smoke默认删除隔离 volume，显式保留时由输出命令管理。

## Open Questions

无；认证、真实 crash/reclaim、PostgreSQL event sink与资源清理边界均已固定。
