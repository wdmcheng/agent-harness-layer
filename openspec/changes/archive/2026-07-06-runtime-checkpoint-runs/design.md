## Context

Phase 3 提供 run/checkpoint storage seam，Phase 4 提供 CanonicalEvent / EventBus / artifact evidence。Phase 5 需要最小 runtime 闭环：创建 run、发布 lifecycle events、持久化 checkpoint、支持 resume 和 idempotency，同时让 API/CLI 使用同一 orchestrator。

## Goals / Non-Goals

**Goals:**
- 用 provider-neutral `RunOrchestrator` 驱动 fake agent run。
- 明确 run state transition 和 terminal status，不允许重复 terminal event。
- checkpoint/resume 通过 repository 和 checkpoint store 完成，进程重启后仍能继续。
- `DBOSRuntimeAdapter` 留在 adapter 边界，业务 agent 和内部 model 不 import DBOS。
- API、CLI、worker shell 共用同一 runtime seam。

**Non-Goals:**
- 不实现真实 LLM/tool execution、approval wait UI 或完整 DBOS-decorated workflow。
- 不在 Phase 5 完成 Phase 13 的物理 API/worker 拆分；但要保留 worker shell 和 service smoke 证据。

## Decisions

- Runtime 内部使用 Pydantic DTO + repository UoW，不传 ORM model 或 DBOS handle。原因：满足跨边界 DTO 规则，并为 service split 保留稳定 seam。
- Local checkpoint store 基于 Phase 3 SQLite/PostgreSQL repository。原因：local/service profile 可共享同一 checkpoint contract。
- DBOS adapter 是可选 wrapper interface。原因：官方 DBOS Python 通过 workflow/step decorator 建模 durable workflow，Phase 5 只证明 adapter boundary，不让 decorator 扩散到业务 agent。
- CLI root 继续使用 `agent-harness`，新增 `run` 子命令。原因：Product-Spec 明确 `agent-harness run <agent_id>`。

## Affected Surfaces

- `agent_harness.runtime`：orchestrator、state、checkpoint、idempotency、fake agent runner。
- `agent_harness.adapters.runtime.dbos`：adapter protocol / no-op service implementation boundary。
- `agent_harness.cli`：新增 run 命令。
- `templates/service-app/app/api/routes/runs.py`：run create/cancel/resume/events route seam。
- `templates/service-app/app/workers/runtime_worker.py`：worker shell。
- `scripts`：local/service smoke 覆盖 run lifecycle。

## Testing Seams

- Module seam：`RunOrchestrator.start_run`、`resume_run`、`cancel_run`。
- Persistence seam：checkpoint 后重新构造 orchestrator 仍可 resume。
- Idempotency seam：同一 key 重复提交返回同一 run。
- CLI seam：`agent-harness run fake-agent` 输出 terminal event。
- API seam：run route 创建 fake run 并可读取 event stream。

## Risks / Trade-offs

- [Risk] fake agent 过于简化 → Mitigation：只把 fake agent 作为 public seam smoke，真实 provider 在 Phase 6。
- [Risk] DBOS adapter 没有真实 workflow proof → Mitigation：Phase 5 只验 boundary 与 no vendor leakage，Phase 13 再做 service adapter 深化。
- [Risk] API/worker 物理拆分提前复杂化 → Mitigation：worker shell 可运行，Phase 13 再验 split process pickup。

## Migration Plan

复用 Phase 3 初始 schema 中的 run/checkpoint 表。新增 runtime 本地 evidence 文件和 fake run records，不需要存量迁移。

## Open Questions

- 无。Phase 13 会补 API/worker 分进程 pickup 的完整 service proof。
