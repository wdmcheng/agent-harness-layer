## 1. Harness Version 公共 Seam

- [x] 1.1 先通过公共模块测试锁定 manifest 必需类别、规范化 checksum/version、secret/不可序列化输入拒绝和 evidence ref 外置，再实现 `harness_versions.py`。

## 2. Experiment 与 Comparison

- [x] 2.1 先通过 deterministic `ExperimentEvaluator` contract 锁定 baseline/candidate 使用相同 split/evaluator profile、baseline-only snapshot 不可后补、candidate missing、partial failure、并发 key 冲突无 orphan split、claim/续租/fencing、续租失败或过期 claim 禁止 terminal 写入、活跃重放无重复 side effect、进程中断或 terminal 写失败转 `needs_review` 且不自动重跑、跨 tenant experiment/comparison 不可见，以及 agent/dataset/split 归属错配 fail closed 且不泄漏 version/score/count/refs，再实现拆分的 experiment application/execution service 和持久化状态转换。
- [x] 2.2 先通过 comparison contract 锁定 per-tag score/delta、holdout delta、regressions、new/fixed failures、`accept|reject|needs_review` 判据，以及 `target_tag_improved`、`named_failure_fixed`、`no_target_improvement`、`holdout_within_threshold`、`holdout_regression_exceeded`、`critical_regression_passed`、`critical_regression_failed`、`new_failures_present`、`local_evidence_incomplete`、`comparison_incomplete` 非空封闭 reason code 集，再实现 comparison service。

## 3. Evidence 与基础链路回归

- [x] 3.1 先通过 local-first/provider adapter contract 锁定 provider degraded、封闭且有界的 evaluator/provider 错误摘要，成功 evaluator result 在 DTO/service 双边界的 secret、本机绝对路径、单项/列表/聚合大小门禁与构造后变异复验，以及顶层与 per-case failure diff 的合法上界 refs 合并后在 DTO/terminal 前压缩为数据库真相引用，再完成 experiment/comparison evidence fan-out。
- [x] 3.2 运行 harness/experiment/comparison、repository integration 与既有 `EvalRunner`/`ScoreSink`/`make eval` 定向测试，记录 approved-only 和 no-approved-cases 无回归证据。
