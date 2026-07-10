## Context

本变更依赖 `eval-dataset-split-foundation` 的稳定 split DTO、三类 Phase 12.5 表和 tenant-scoped repository。现有 `EvalRunner`/`ScoreSink` 证明 approved-only 与 local-first provider fan-out，但它们面向单 dataset run；实验层需要组合两个 harness run 并产出对比证据，而不是改变基础 runner。完整依赖与共享文件见 `../phase-12-5-change-matrix.md`。

## Goals / Non-Goals

**Goals:**
- 用不可变 manifest 标识会改变 Agent 行为的全部 harness 输入。
- 在相同 split/evaluator 条件下运行 baseline/candidate，并生成确定性 comparison。
- 把 local evidence 作为真相源，provider 仅作可降级 fan-out。
- 通过公共 DTO/protocol 供 API/CLI change 调用，不暴露 evaluator/provider 实现。

**Non-Goals:**
- 不负责 HTTP/CLI auth/policy/audit，也不写 accepted harness record。
- 不修改 prompt、tool/config 文件，不实现自动调参或 release gate。

## Decisions

1. **Harness version 是规范化 manifest 的 SHA-256，不是自由文本版本号。** manifest 为每类输入保存 checksum、可选 evidence ref 和短 diff summary，map key 与 list 按契约规范化后编码。备选由调用方任意传 version 字符串无法证明 baseline/candidate 实际输入。
2. **新增 `ExperimentEvaluator` protocol 适配现有 `EvalRunner`。** protocol 输入明确的 approved case ids、harness manifest 和 evaluator profile，输出 provider-neutral per-case scores/refs；生产 adapter 复用现有 scoring seam，测试使用 deterministic fake。备选直接把 `EvalRunner` 改成实验状态机会破坏 Phase 11 的简单 `make eval` 路径。
3. **`ExperimentService` 负责编排，repository 负责原子状态更新。** 服务读取 split、运行 baseline/candidate、先写本地结果，再 fan-out provider；每个状态转换携带 tenant 和 expected current status，避免重试覆盖 terminal evidence。
4. **Comparison 纯粹消费已持久化结果。** 每个 tag 对 optimization/holdout 分别聚合，regression policy 定义关键 case、允许 holdout delta 和 failure threshold；输出 `accept|reject|needs_review` 与 reason codes。备选让 provider 计算 recommendation 会使本地证据在 provider 失败时不可用。
5. **完整 per-case diff 超阈值时外置。** 公共 record 只保存聚合与 refs，复用 artifact/telemetry redaction 边界，provider adapter 只收已脱敏 DTO。Recommendation 的完整性只强制 local evidence；provider ref 可在记录脱敏 degraded status 后缺失。

## Affected Surfaces

- `agent_harness/evals/harness_versions.py`：manifest、input refs、version builder。
- `agent_harness/evals/experiments.py`：request/result/comparison DTO、evaluator/evidence protocols、ExperimentService。
- `agent_harness/evals` exports 与 service-app composition helper。
- `eval_experiment_repositories.py`：消费上游 repository 的 experiment 状态、score/comparison/evidence 更新 seam；不修改 migration。人工 decision 由下游独立 `evals/acceptance.py` 编排，本 change 不回收该文件所有权。
- 现有 `ScoreSink`/`TelemetryFacade` adapter：仅通过 provider-neutral payload 复用。

## Testing Seams

- 公共模块：manifest canonicalization/secret rejection、recommendation 与 per-tag/holdout/regression 聚合。
- ExperimentEvaluator protocol：deterministic fake 证明相同 split、case membership 和 evaluator profile。
- Repository integration：状态转换、partial failure、tenant isolation、local evidence 先写。
- Provider adapter contract：异常、secret-shaped error、大 payload externalization。
- 回归：现有 `EvalRunner`、`make eval` 和 no-approved-cases 不改行为。

## Risks / Trade-offs

- [Evaluator 对不同 harness 输入仍可能不确定] → comparison 固定 split、evaluator profile 与 metric versions；fake/local profile 提供确定性 contract proof。
- [平均分掩盖关键 failure] → recommendation 同时检查 per-tag、holdout、关键 regression 和 failure diff，不能只看 aggregate。
- [Provider 写入延迟或失败] → 本地 transaction 先完成；provider status 独立追加，不回滚 comparison。
- [manifest 泄漏 prompt/secret] → 仅存 checksum、短脱敏 diff 和 refs，构建前执行现有 redaction/secret guard。

## Migration Plan

本变更不创建新 revision，使用上游 `0009` 已建立的 experiment 表。部署顺序必须先应用上游 migration 和 split/repository，再启用 ExperimentService。代码回滚保留 experiment evidence；旧版本忽略新增表，禁止删除已有对比记录。

## Open Questions

无；初始 metric 聚合、recommendation 三态和 provider degradation 由 EVL-004 与上游控制文档确定。
