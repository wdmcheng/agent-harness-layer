## 1. Harness Version 公共 Seam

- [ ] 1.1 先通过公共模块测试锁定 manifest 必需类别、规范化 checksum/version、secret/不可序列化输入拒绝和 evidence ref 外置，再实现 `harness_versions.py`。

## 2. Experiment 与 Comparison

- [ ] 2.1 先通过 deterministic `ExperimentEvaluator` contract 锁定 baseline/candidate 使用相同 split/evaluator profile、baseline-only snapshot 不可后补、candidate missing、partial failure、跨 tenant experiment/comparison 不可见，以及 agent/dataset/split 归属错配 fail closed 且不泄漏 version/score/count/refs，再实现 experiment runner/service 和持久化状态转换。
- [ ] 2.2 先通过 comparison contract 锁定 per-tag score/delta、holdout delta、regressions、new/fixed failures、`accept|reject|needs_review` 判据，以及 `target_tag_improved`、`named_failure_fixed`、`no_target_improvement`、`holdout_within_threshold`、`holdout_regression_exceeded`、`critical_regression_passed`、`critical_regression_failed`、`new_failures_present`、`local_evidence_incomplete`、`comparison_incomplete` 非空封闭 reason code 集，再实现 comparison service。

## 3. Evidence 与基础链路回归

- [ ] 3.1 先通过 local-first/provider adapter contract 锁定 provider degraded、secret-shaped error redaction 和大 payload evidence ref，再完成 experiment/comparison evidence fan-out。
- [ ] 3.2 运行 harness/experiment/comparison、repository integration 与既有 `EvalRunner`/`ScoreSink`/`make eval` 定向测试，记录 approved-only 和 no-approved-cases 无回归证据。
