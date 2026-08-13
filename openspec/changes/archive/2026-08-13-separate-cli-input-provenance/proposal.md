## Scope Guard

这是 Phase 20 之后临时发现的既有 CLI Bug 修复，不开启 Phase 21。该 change 不修改 `DEV-PLAN.md`，不增加公开 schema、queue 协议、部署能力或回滚平台。

## Source Links

- Product-Spec.md: `FLOW-001`、`FLOW-003`、`REQ-001`、`REQ-003` 及业务 input/source/trust 不变量
- API-Contract.md: `CLI-RUN-001`、`RUN-001`、approval continuation 与 terminal evidence 语义
- ADR: `docs/adr/0001-p0-service-boundaries.md` 的 API/worker 私有 execution context 与稳定 queue DTO 边界

## Why

核心 CLI 为标记调用来源而把 `source=cli` 注入业务 input。严格业务 DTO 因此会把 transport 字段当成未知业务字段；允许该字段又会污染 prompt、provider request、公开 input 与 delegation 语义。来源属于可信 execution provenance，不属于调用方业务数据。

同时，queued rebuild、幂等重放、terminal recovery 与 approval resume 会从私有持久化 context 恢复来源和 nullable execution request id。approval resume 重建 executor context 时若使用新入口或组件临时生成的 request id，而不是已分类私有 context 中的 authoritative execution request id，就会破坏原 execution/usage correlation。APR-002 resolution operation 与新 `run.resumed`/terminal event 仍使用本次 resume request id，保持主规格的当前入口关联语义。

## What Changes

- CLI 不再向业务 input 注入、删除或保留特殊 `source` 字段；调用方显式提交的同名业务字段仍按业务 schema 处理。
- 增加只允许 `source=cli` 的封闭 typed provenance，并只在必要的私有 execution context 中以 exact `run-input-provenance-v1` envelope 持久化和分类。
- local/service 首次创建、幂等重放、queued rebuild、terminal recovery 与 approval resume 通过同一私有 classifier/repository seam 恢复 provenance 和 authoritative nullable execution request id。
- guardrail、audit 与 executor 接收可信 provenance；业务 input、公开 run DTO、HTTP/OpenAPI schema、provider request 和 delegation input/hash 语义保持不变。
- `RunInputProvenance` 只定义在内部下划线模块，不从 `agent_harness.runtime` 导出；公开 `RunOrchestrator.start_run` 的参数集合保持不变，CLI 只通过显式私有 submission seam 交付受信 execution context。
- approval resume 由现有 approval lease 取得当前 resolution request id，并经不改变公开 `resume_run` 参数集合的私有 continuation seam 传递；重建的 executor/continuation context 使用分类结果中的 authoritative execution request id，APR-002 resolution queue correlation 与新 resumed/terminal event 使用当前 resolution request id，两个 correlation 平面不得互相覆盖。

## Non-Goals

- 不新增 `ClaimableRunQueue`、Redis inspect/fenced claim、PostgreSQL row-lock claim 或任何 durable queue 协议。
- 不改变既有 `RunQueue` pickup/reclaim、message DTO、DBOS、consumer ownership 或 worker compatibility 语义。
- 不增加旧 worker compatibility/rollback readiness、两级 broker、shared/exclusive fence、Registry/pinned image、proof-tree-wheel FD、Docker event 状态机、rollback AST gate或大规模 hostile matrix。
- 不改变公开 HTTP/CLI input schema、provider adapter request、delegation request/hash、storage schema 或 migration。
- 不把聚焦 classifier/recovery 测试变成生产验证服务或运维入口。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `runtime-checkpoint-runs`: CLI 可信来源与业务 input 分离，并在必要恢复路径复用私有 typed context。
- `durable-run-queue`: 已有 queue delivery 后从 durable run context 恢复可信 provenance；queue 协议本身不变。

## Impact

- 生产实现独占范围：`packages/agent-harness/src/agent_harness/cli.py`、`policy/engine.py`、`approvals/_continuation.py`、`approvals/_queue_resolution.py`、`runtime/executor.py`、新建 `runtime/_continuation_context.py`、`runtime/_run_lifecycle.py`、`runtime/_queued_run_orchestration.py`、`runtime/_run_continuation.py`、`storage/run_repositories.py`、`storage/repositories.py`，以及只删除既有 transport `source` 特判的四个模板适配层：`agents/examples/dev_assistant/agent.py`、`rag_assistant/agent.py`、`repo_analyst/agent.py`、`ticket_triage/agent.py`。`approvals/_queue_resolution.py` 只允许在既有DBOS确定失败收口中把持久化resolution request id交给现有私有terminal evidence恢复seam。
- 串行共享验收文件：`templates/service-app/app/workers/runtime_worker.py`。仅在 harden 的 profile/profiles-dir/once/env-file hunk完成并转绿后，保留既有业务 input，并验证 worker 经 `execute_run` 进入 orchestrator classifier；DBOS 确定性失败的 terminal recovery 只把既有 queue message 当前 `request_id` 转发给 orchestrator 私有失败收口 seam，不增加 private context 参数、queue DTO、claim 或其他协议。随后复验 harden 与 separate 两组聚焦合同。
- 测试独占范围：`tests/contracts/test_cli_input_provenance_contracts.py`、`tests/contracts/test_cli_input_provenance_recovery_contracts.py`。
- 联合裁剪范围：三张契约票通过并建立仓库外快照后，由主 Agent 独占执行 change matrix 的 `joint-crop-v1`；它只恢复51个 tracked 膨胀文件的既有 HEAD 内容、删除39个 untracked 膨胀文件并逐 hunk 裁剪21个保留文件，不增加 queue、runtime 或运维能力。`approvals/_continuation.py` 在预裁剪时为 clean，不计入 dirty manifest，裁剪后才按本 change 独占 owner 修改；`approvals/_queue_resolution.py` 是51个 tracked 恢复路径之一，裁剪时先恢复 HEAD，随后才按独占 owner 写入 resolution request-id 的直接因果 hunk。
- `agent_harness.runtime` 公开 export、`RunOrchestrator.start_run` 与 `resume_run` 参数、HTTP/OpenAPI、provider request、delegation、queue DTO/协议、数据库 schema、依赖与 UI：无变化。
