## Source Links

- Product-Spec.md: REQ-016 的 baseline run、candidate harness run、regression report 与 acceptance evidence 要求。
- DEV-PLAN.md: Phase 12.5 的 experiment、harness version、per-tag comparison、holdout 与 regression 交付项。
- API-Contract.md: EVL-004 comparison response、provider degraded 和 evidence redaction 契约。
- docs/eval-observability-loop.md: Phase 12.5 实验闭环、Harness Version 输入和 Acceptance Gate 判据。
- Design-Brief.md or design artifact: 不涉及 UI，无设计稿依赖。
- CONTEXT.md / ADR: 当前仓库没有适用于本变更的领域上下文或 ADR。

## Why

单次 eval 总分无法证明 harness 变更有效，也无法发现目标行为之外的退化。基于合格 split 运行 baseline/candidate、按标签比较并保留 holdout/regression evidence，才能为人工接受提供防过拟合的可复核证据。

## What Changes

- 建立覆盖 prompt/instruction、tool description、agent/retrieval/policy config、model/adapter settings 的 `harness_version` metadata 与稳定标识。
- 在同一 tenant、agent、dataset split 上运行和持久化 baseline/candidate experiment，记录 score summary、regression summary 与 local/provider evidence refs。
- 生成 per-tag baseline/candidate score、delta、holdout delta、regressions、new failures、fixed failures 和 acceptance recommendation。
- local evidence 先于 provider fan-out 落盘；provider 失败只产生脱敏 degraded summary，不删除 experiment/comparison evidence。
- 大 payload 和 provider 原始响应只保留脱敏摘要或 evidence ref，不进入公共 DTO。

## Non-Goals

- 不增加 EVL-004 HTTP routes 或 CLI composition。
- 不代表人工 reviewer 接受 candidate，不自动写 accepted production harness，也不改写 prompt、tool description 或配置。
- 不改变基础 `make eval` approved-only 与 no-approved-cases 语义。
- 不涉及 Phase 13 API/worker 分进程或 Phase 15 release gate。

## Capabilities

### New Capabilities

- `eval-harness-experiments`: 定义 harness version、baseline/candidate experiment、按标签 comparison、回归 evidence 与 provider degradation。

### Modified Capabilities

- 无。

## Impact

- 核心包：新增 `evals.harness_versions`、`evals.experiments` 公共 DTO、runner/comparison service 和 local-first evidence seam。
- 存储：消费 `eval-dataset-split-foundation` 提供的 Phase 12.5 repository/UoW 与 experiment record。
- 观测：复用 `TelemetryFacade`/provider-neutral adapter，只传递已脱敏摘要与 refs。
- 测试：新增 harness version、baseline/candidate、per-tag/holdout/regression、provider degraded、secret 和大 payload contract tests。
- 下游依赖：`eval-experiment-api-acceptance` 只通过本变更的公共 service/DTO 暴露 EVL-004。
