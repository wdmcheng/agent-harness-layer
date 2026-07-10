# Phase 12.5 OpenSpec 关系矩阵

## 依赖 DAG

```text
eval-dataset-split-foundation
  -> eval-harness-experiment-comparison
    -> eval-experiment-api-acceptance
```

三个 change 共享 Phase 12.5 的验收、持久化 schema 和公共 DTO，属于关联变更。任何实现开始前，必须完成每个 change 的严格校验与独立变更契约审查，并由新的 code-reviewer 对全部 artifacts 和上游真相源完成联合 Stage 1/2 PASS。

## 关系与所有权

| Change | 直接依赖 | 主要公共 seam | 主要文件所有权 | 共享验收 / 冲突控制 |
|---|---|---|---|---|
| `eval-dataset-split-foundation` | 无 | behavior tag、split DTO/service、Phase 12.5 repository/UoW | `evals/datasets.py`、Phase 12.5 ORM/repository/migration | 唯一创建 Phase 12.5 schema；不得实现 experiment scoring 或 API/CLI |
| `eval-harness-experiment-comparison` | `eval-dataset-split-foundation` | harness version、experiment runner、comparison DTO/service、local-first evidence | `evals/harness_versions.py`、`evals/experiments.py` | 只消费已校验 split；产生 acceptance recommendation，但不得代表人工接受 |
| `eval-experiment-api-acceptance` | 前两个 change | EVL-004 HTTP/CLI、identity/policy/audit、idempotent acceptance | `evals/acceptance.py`、`app/api/routes/evals.py`、CLI composition、EVL-004 schemas/tests | 只能调用公共 experiment/repository seam；不得回改 experiment 算法、直接操作 ORM 或自动改写 harness 输入 |

共享文件 `evals/__init__.py`、storage export/UoW composition、`docs/eval-observability-loop.md`、`DEV-PLAN.md` 和组合测试按 DAG 顺序修改；后一个 change 必须保留前一个 change 已审查的公开行为。任何共享接口变化都会使受影响 change 的契约审查与联合审查失效，必须从 Stage 1 重跑。

## 共享验收

- 只有 approved、无 secret、标签完整且同 tenant/agent/dataset 的 case 可以进入 split 和 experiment。
- baseline/candidate 必须使用同一 split 和 evaluator profile；comparison 必须给出 per-tag、holdout、regression 与 failure diff evidence。
- provider failure 必须保留本地 experiment/comparison evidence，不得泄漏 secret、provider 原始响应或完整大 payload。
- accepted production binding 只能源于完整 comparison、人工 reviewer、policy allow 和 audit，且 version 必须与已比较 candidate 完全一致；rejected decision 使用同一不可变 review decision seam 但不得产生 production binding；任何 decision 都不得自动改写 prompt、tool description 或生产配置。
- Phase 11 的 draft -> approve、`make eval` approved-only 和 no-approved-cases 语义必须无回归。

## 允许实现顺序

1. `eval-dataset-split-foundation` 建立标签、split 与共享持久化边界。
2. `eval-harness-experiment-comparison` 在稳定 split/repository seam 上实现实验与比较。
3. `eval-experiment-api-acceptance` 通过前两者的公共 seam 暴露 API/CLI 和人工接受门禁。

不允许跳过上游 change，也不允许把 Phase 13 API/worker split 或 Phase 15 release automation 塞入任一 change。
