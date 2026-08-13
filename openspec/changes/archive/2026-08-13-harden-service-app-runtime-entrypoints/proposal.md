## Scope Guard

这是 Phase 20 之后临时发现的既有 Bug 修复，不开启 Phase 21。该 change 不修改 `DEV-PLAN.md`，不新增部署能力、验证平台、生产协议或运维入口。

## Source Links

- Product-Spec.md: `SCOPE-003`、`FLOW-001`、`REQ-003`、`AC-003`、`AC-006`、`AC-007`
- API-Contract.md: 本 change 不修改产品公开 `agent-harness` CLI、HTTP 或 module schema；复制模板内部 worker CLI 向后兼容新增可选 `--env-file`
- ADR: `docs/adr/0001-p0-service-boundaries.md` 的 service-app 与 runtime composition 边界

## Why

可复制 service-app 的 `make worker` 没有把调用方选择的 profile 与 profiles 目录完整传给 worker，非 service profile 也可能误入常驻消费；app/runtime/worker 对 env file 的选择不一致时，还会重新发现 ambient `.env`。结果是同一复制模板在不同当前目录或宿主环境下得到不同运行配置。

## What Changes

- `make worker` 显式传递 `--profile` 与 `--profiles-dir`；非 `service` profile 增加 `--once`，`service` profile 保持常驻。
- `make worker` 在 `ENV_FILE` 非空时显式传递同一 `--env-file`，省略或空值时保留既有默认发现语义。
- `create_app`、runtime composition 与 worker 的 parse/run-once/run-forever 路径支持同一可选显式 env file，并逐层传给既有类型化配置加载器。
- 复制模板与 app surface 聚焦测试使用测试专属空 env file，证明 ambient `.env` 不会污染显式配置路径。

## Non-Goals

- 不增加 service-smoke call-count、HMAC、角色 capability、trace producer 或边界 guard 平台。
- 不增加两级 broker、shared/exclusive fence、旧 worker compatibility/rollback readiness、Registry/pinned image/proof-tree-wheel FD、Docker event 状态机或 rollback AST gate。
- 不修改 queue、provider、credential、外部 tool、observability/SaaS 或 service deployment 协议。
- 不把离线聚焦测试提升为生产控制面或运维能力。
- 不处理 CLI input provenance；该 Bug 由 `separate-cli-input-provenance` 负责。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `service-app-shell`: 修正模板 worker 与 app/runtime/worker 的显式配置入口。

## Impact

- 生产实现独占范围：`templates/service-app/Makefile`、`templates/service-app/app/main.py`、`templates/service-app/app/runtime.py`。
- 串行共享验收文件：`templates/service-app/app/workers/runtime_worker.py`。本 change 先且只拥有 profile/profiles-dir/once/env-file 接线 hunk；后置 `separate-cli-input-provenance` 不得改该配置 hunk，也不得增加 private context 或 queue 协议，只允许把既有 queue message 的当前 `request_id` 传入既有 queued terminal recovery 私有 seam，并验证既有 `execute_run` 仍进入 orchestrator classifier。两项完成后必须复验本 change 的聚焦合同。
- 测试独占范围：`templates/service-app/tests/test_app_surface.py`、`tests/contracts/test_service_app_runtime_entrypoint_contracts.py`。
- 联合裁剪范围：三张契约票通过并建立仓库外快照后，由主 Agent 独占执行 change matrix 的 `joint-crop-v1`；它只恢复51个 tracked 膨胀文件的既有 HEAD 内容、删除39个 untracked 膨胀文件并逐 hunk 裁剪21个保留文件，不授予本 change 新生产能力。
- 产品公开 `agent-harness` CLI、公开 API、存储 schema、queue 协议、依赖、部署拓扑和 UI：无变化。模板内部 `python -m app.workers.runtime_worker` CLI 仅新增向后兼容的可选 `--env-file`，既有调用方无需迁移。
