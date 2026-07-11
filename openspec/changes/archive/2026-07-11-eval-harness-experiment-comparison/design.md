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
3. **`ExperimentService` 与独立 execution coordinator 编排，repository 负责原子状态更新。** 创建 split、experiment 与首个 execution claim 在同一 transaction 提交；协调器以 claim id 作为 fencing token 并续租。续租返回失败或抛异常会传播为 claim 丢失，协调器在任何 terminal 写入前停止 heartbeat 并 fail closed。Evaluator 结果只能通过 `tenant/id/status=running/claim_id=owner/lease=unexpired` 的单次条件更新落为本地 terminal，零行即 `execution_fenced`；`needs_review` 和任何 terminal 都不可被迟到 worker 覆盖。之后 provider fan-out 只通过独立、限定 expected local terminal status 的摘要追加接口写 provider status/comparison 摘要，不再改写 eval evidence 或复用 execution claim。活跃 `running` 重放只返回已持久化状态；续租失败、租约过期、进程中断、evaluator 已返回但 terminal 写入失败，或旧 0009 遗留的可见 `created`，都无法证明外部副作用是否完成，必须 fenced 地转为 `needs_review`，不得 takeover 后自动重跑 evaluator/provider。新实现的 `created` 只存在于 create+claim 的未提交同一事务内，不作为可重放状态。
4. **Comparison 纯粹消费已持久化结果。** 每个 tag 对 optimization/holdout 分别聚合，regression policy 定义关键 case、允许 holdout delta 和 failure threshold；输出 `accept|reject|needs_review` 与 reason codes。备选让 provider 计算 recommendation 会使本地证据在 provider 失败时不可用。
5. **完整 per-case diff 超阈值时外置，成功 evaluator evidence 同样不可信。** 公共 record 只保存聚合与 refs；case/local refs 在 DTO 与 service 边界双重执行 secret、本机绝对路径、单项长度、列表数量和聚合大小校验，防止 adapter 在构造后变异绕过。各 evaluator 列表合法但经 baseline/candidate/comparison 顶层或同一 case 的 failure diff 合并后超过公共 100 项或 16 KiB 时，必须在 DTO 构造与 terminal 持久化前稳定压缩为 `db://eval-experiments/<id>` 真相引用；完整 refs 保留在数据库 score summary，不得先写入失败或不可读终态再仅在响应截断。Provider adapter 只收已验证、已脱敏且有界的 persisted DTO。Recommendation 的完整性只强制 local evidence；provider ref 可在记录脱敏 degraded status 后缺失。

## Affected Surfaces

- `agent_harness/evals/harness_versions.py`：manifest、input refs、version builder。
- `agent_harness/evals/experiments.py`、`experiment_execution.py`、`experiment_persistence.py`：应用服务、claim/lease/fencing/`needs_review` 执行状态机与 terminal persistence；DTO、validation、record mapping 和 provider fan-out 按职责拆分。
- `agent_harness/evals` exports 与 service-app composition helper。
- `eval_experiment_repositories.py`：消费上游 repository 的 experiment 状态、score/comparison/evidence 更新 seam；不修改 migration。人工 decision 由下游独立 `evals/acceptance.py` 编排，本 change 不回收该文件所有权。
- 现有 `ScoreSink`/`TelemetryFacade` adapter：仅通过 provider-neutral payload 复用。

## Testing Seams

- 公共模块：manifest canonicalization/secret rejection、recommendation 与 per-tag/holdout/regression 聚合。
- ExperimentEvaluator protocol：deterministic fake 证明相同 split、case membership 和 evaluator profile。
- Repository integration：原子 split+experiment+claim、并发同 key、续租失败/异常与过期租约 fencing、进程中断、terminal 写失败转 `needs_review`、partial failure、tenant isolation、local evidence 先写。
- Evaluator/provider adapter contract：成功与失败结果的 secret/绝对路径/大小门禁、构造后变异复验、大 payload externalization。
- 回归：现有 `EvalRunner`、`make eval` 和 no-approved-cases 不改行为。

## Risks / Trade-offs

- [Evaluator 对不同 harness 输入仍可能不确定] → comparison 固定 split、evaluator profile 与 metric versions；fake/local profile 提供确定性 contract proof。
- [平均分掩盖关键 failure] → recommendation 同时检查 per-tag、holdout、关键 regression 和 failure diff，不能只看 aggregate。
- [Provider 写入延迟或失败] → 本地 transaction 先完成；provider status 独立追加，不回滚 comparison。
- [执行中断后无法判断 evaluator/provider 是否已有副作用] → 不自动接管过期 `running`；持久化 `needs_review` 并要求维护者根据本地/外部 evidence 决定后续，不以可用性换重复副作用。
- [manifest 泄漏 prompt/secret] → 仅存 checksum、短脱敏 diff 和 refs，构建前执行现有 redaction/secret guard。

## Migration Plan

本变更发现执行幂等必须持久化 claim，但上游 `0009` 已存在可应用历史，因此不改写 0009，而由 foundation schema 所有权追加 `0010_eval_experiment_execution_claims`；识别 legacy created 风险后继续追加 `0011_eval_experiment_legacy_created_review`，不改写已应用 0010。部署顺序必须依次应用 0009/0010/0011 和 split/repository，再启用 ExperimentService；0010 补 nullable claim/expiry，0011 保留 terminal evidence并把结果不确定的 legacy `created` 转 `needs_review`。代码回滚保留 experiment evidence；非空 Phase 12.5 表禁止 downgrade，生产采用 forward fix。

## Open Questions

无；初始 metric 聚合、recommendation 三态和 provider degradation 由 EVL-004 与上游控制文档确定。
