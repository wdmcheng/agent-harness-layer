## Purpose

定义不可变 harness version、baseline/candidate experiment、可复核 comparison 与 local-first evidence 安全边界。

## Requirements

### Requirement: Harness version 完整描述行为输入
系统 SHALL 为 baseline 和 candidate 建立不可变 `harness_version`，覆盖 prompt/instruction、tool description、agent config、retrieval config、policy defaults、model profile/adapter settings。每类输入 MUST 记录规范化 checksum、脱敏 diff summary 或 evidence ref；相同规范化 manifest MUST 产生相同 version id，缺少必需类别、包含 secret 或不可序列化 provider object MUST 被拒绝。

#### Scenario: 相同 manifest 产生相同版本
- **WHEN** 两次构建包含相同规范化 harness 输入但 mapping/list 原始顺序不同
- **THEN** 系统产生相同 `harness_version` id，并保留相同的输入 checksum 与 evidence refs

#### Scenario: Secret 不进入 harness metadata
- **WHEN** prompt、tool description 或 config metadata 包含 secret fixture
- **THEN** 系统拒绝创建 harness version 或只接受已有脱敏 artifact ref，公共 manifest、错误和 local/provider evidence 均不包含原始 secret

### Requirement: Baseline 与 candidate 在同一 split 上运行
系统 SHALL 在同一 tenant、agent、dataset split、evaluator profile 和 metric 集合上执行 baseline/candidate experiment。experiment MUST 记录 `experiment_id`、request/trace 关联、split id、baseline 和可选 candidate harness version、eval run refs、per-case/per-tag score summary、regression summary、local/provider evidence refs 和状态。创建 split、experiment 与首个私有 execution claim MUST 原子提交；claim id MUST 作为结果写入的 fencing token 并带有限租约。续租返回失败或抛出异常 MUST 视为 claim 丢失。Evaluator 结果写入 MUST 原子要求 tenant/id 匹配、当前状态为 `running`、claim id 等于 owner 且租约尚未过期；更新零行 MUST 失败为 `eval.experiment.execution_fenced`，`needs_review` 或其他 terminal 记录 MUST 不可被迟到 worker 覆盖。Provider 摘要 MUST 在本地 terminal 后通过独立 expected-status 更新追加，不得改写既有 eval evidence。省略 candidate 时只产生不可变 baseline snapshot，不支持在原 experiment 上后补；需要 comparison 时调用方 MUST 新建同时携带 baseline/candidate 的 experiment。comparison 前 candidate MUST 存在且完成。活跃 `running` 重放 MUST 不调用 evaluator/provider；续租失败、租约过期、进程中断或 evaluator 已执行但 terminal 结果无法持久化时，系统 MUST 转为 `needs_review` 且不得自动重跑不确定副作用。

#### Scenario: Baseline 与 candidate 使用相同 case membership
- **WHEN** experiment 对同一 split 运行 baseline 和 candidate
- **THEN** 两个 run 消费完全相同的 optimization、holdout、regression case ids 和 evaluator profile，draft 或 split 外 case 不参与评分

#### Scenario: Candidate 缺失时不能比较
- **WHEN** experiment 只有 completed baseline、没有 candidate harness 或 candidate run
- **THEN** comparison 返回稳定 candidate-missing 状态，不伪造 delta、recommendation 或 acceptance evidence；该 baseline snapshot 保持只读，调用方必须新建带 candidate 的 experiment 才能比较

#### Scenario: Eval 执行失败保留已有 evidence
- **WHEN** candidate evaluator 在部分 cases 后失败
- **THEN** experiment 记录 failed/degraded 状态、已完成 case refs 和脱敏错误摘要，baseline 与已落盘 local evidence 不被删除

#### Scenario: 活跃重放不重复执行
- **WHEN** 相同 tenant、idempotency key 和规范化 body 在原 experiment 仍持有有效 execution claim 时重放
- **THEN** 系统返回同一 `experiment_id` 和已持久化 `running` 状态，不调用第二次 evaluator 或 provider，也不创建第二个 split 或 eval run

#### Scenario: 不确定执行进入人工复核
- **WHEN** execution claim 续租返回失败或抛出异常、租约过期、进程在 evaluator 调用期间中断，或 evaluator 已返回但 terminal 结果提交失败
- **THEN** 系统 fenced 地持久化 `needs_review`、清除私有 claim，并在后续幂等重放中返回同一状态且 evaluator/provider 调用数不增加

#### Scenario: 迟到 worker 不可覆盖终态
- **WHEN** owner 已被转为 `needs_review`、claim 已过期被复核流程接管，或 experiment 已写任一 terminal 后，旧 evaluator 携原 claim 迟到返回
- **THEN** 原子结果更新返回 `eval.experiment.execution_fenced`，既有 status、score、comparison、local refs 与 provider summary 完全不变

### Requirement: Comparison 输出按标签、holdout 与 regression 的可复核差异
系统 SHALL 基于同一 experiment 的 baseline/candidate results 生成 comparison。结果 MUST 包含每个请求标签的 baseline score、candidate score、delta，整体 holdout delta、regressions、new failures、fixed failures、regression subset 结果、`acceptance_recommendation` 和非空 `recommendation_reason_codes`。`holdout_delta` MUST 为 candidate aggregate score 减 baseline aggregate score；`holdout_delta < -regression_policy.max_holdout_regression` 时 MUST 判定超过允许退化阈值。`critical_case_ids` 以及行为标签命中 `critical_tags` 的 regression case MUST 全部通过才可产生 `critical_regression_passed`。recommendation MUST 为 `accept`、`reject` 或 `needs_review`；reason codes MUST 只使用以下封闭字面值：`target_tag_improved`、`named_failure_fixed`、`no_target_improvement`、`holdout_within_threshold`、`holdout_regression_exceeded`、`critical_regression_passed`、`critical_regression_failed`、`new_failures_present`、`local_evidence_incomplete`、`comparison_incomplete`。它只供人工 review，不构成 acceptance side effect。

#### Scenario: 目标标签改善且门禁通过时建议接受
- **WHEN** candidate 的目标标签分数提升或修复命名 failure mode，holdout 未超过允许退化阈值，且关键 regression 全部通过
- **THEN** comparison 返回 `acceptance_recommendation="accept"`、`recommendation_reason_codes` 和对应 score/evidence refs，但不创建 accepted harness record

#### Scenario: Holdout 明显退化时拒绝建议
- **WHEN** candidate 的 optimization 总分上升但 holdout delta 超过 regression policy 允许阈值
- **THEN** comparison 返回 `acceptance_recommendation="reject"` 和稳定 holdout/regression reason codes，列出 holdout regressions/new failures，且不得以总分上涨覆盖该结论

#### Scenario: 本地 Evidence 不完整时需要人工复核
- **WHEN** 任一请求标签缺少 baseline/candidate score、关键 regression 没有结果或必需 local evidence refs 不完整
- **THEN** comparison 返回 `needs_review`、local-evidence-incomplete reason code 或稳定 incomplete-evidence error，不产生可接受结论；provider refs 在存在脱敏 degraded status 时允许缺失且不得单独阻塞 local-first recommendation

### Requirement: Experiment evidence local-first 并安全降级 provider
系统 SHALL 在 provider fan-out 前持久化本地 experiment run 和 comparison evidence。provider failure MUST 只追加脱敏 degraded status，不得删除或隐藏 local evidence、experiment record 或 comparison。Evaluator/provider 异常进入持久化或公共 DTO 时 MUST 映射为封闭 error code 和有界通用摘要，原始大响应不得内联。公共/provider payload MUST 不包含 secret、完整 case/trace payload、provider raw response、绝对路径或 provider SDK object。

#### Scenario: Provider failure 不丢 comparison
- **WHEN** Logfire、Phoenix、Langfuse 或 OTel adapter 写入 comparison 时失败
- **THEN** 调用返回 completed-with-degradation 摘要与 local evidence refs，comparison 内容仍可读取，错误消息已脱敏

#### Scenario: 大 comparison payload 使用 evidence ref
- **WHEN** per-case failure diff 超过 inline 阈值
- **THEN** 公共 comparison 只返回聚合、截断摘要和 artifact/evidence ref，完整 payload 不进入 API DTO 或 provider payload

#### Scenario: 合并后的 evidence refs 保持公共边界
- **WHEN** baseline-only 恰有 100 个合法 refs，baseline/candidate 顶层各自合法但合并后超过 100 项或 16 KiB，或同一 case 两侧合法 refs 在 failure diff 合并后超过该边界
- **THEN** 系统在 comparison DTO 构造与 terminal 持久化前把公共、failure diff、comparison 与 provider 共用 refs 稳定压缩为数据库真相引用，完整 evaluator refs 保留在本地 score summary，create/read/comparison/replay/CLI 均保持可读

### Requirement: Experiment 与 comparison 严格 tenant 隔离
experiment service SHALL 只读取请求 tenant 可见的 split、cases、runs、comparison 和 evidence；agent/dataset/split 归属不匹配 MUST fail closed。不存在与跨 tenant 资源 MUST 使用相同不可见语义，避免泄漏资源标识和计数。

#### Scenario: 跨 tenant experiment 不可读取
- **WHEN** tenant A 读取或比较 tenant B 的 experiment id
- **THEN** 系统返回资源不可见错误，且不暴露 tenant B 的 harness version、score、case count 或 evidence refs
