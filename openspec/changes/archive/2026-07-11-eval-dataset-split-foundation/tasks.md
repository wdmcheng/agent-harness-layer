## 1. 标签与 Split 公共 Seam

- [x] 1.1 先通过 `agent_harness.evals` 公共模块测试锁定 behavior tag 规范化、approved/secret/tenant 合格性和稳定错误，再实现 tag 查询、过滤与 fail-closed eligibility。
- [x] 1.2 先通过公共 split service 测试锁定 `deterministic_multilabel_v1`、0.8/0.2 默认值、ratio 范围/求和、regression policy 字段与非法/跨归属 ref、regression 预留、确定性多标签 optimization/holdout 分配、非空门禁和可复现 membership，再实现 `datasets.py` DTO/service。

## 2. Phase 12.5 持久化基础

- [x] 2.1 先通过 SQLite migration/model contract 锁定 `eval_dataset_splits`、`eval_experiments`、`harness_acceptance_records` 的字段，尤其是 experiment 的 `(tenant_id, idempotency_key)` 唯一约束、request hash、evaluator profile、metric versions，以及 decision request hash、nullable accepted version、production binding 和 `0008 -> 0009` 基础链；再以追加式 `0010` 锁定私有 execution claim/expiry，并以 `0011` 数据迁移证明已有 0009 terminal evidence 原地升级不变、legacy `created` 转 `needs_review` 且不自动重跑、任一 Phase 12.5 evidence 存在时 downgrade fail closed。
- [x] 2.2 先通过 repository/UoW 集成测试锁定 split/experiment create/get/update、原子 claim/续租/fencing、过期或结果不确定执行转 `needs_review` 且不可自动重跑、每个 experiment 唯一不可变 accepted/rejected decision、同 reviewer 同 body 幂等、跨 reviewer/decision/version 冲突、原子 audit、tenant isolation 和无完整 payload DTO，再实现拆分的 split/experiment/acceptance repositories 与 composition。

## 3. 集成与回归证据

- [x] 3.1 导出新的公共 DTO/protocol，运行 tag/split、真实 ExperimentService create、SQLite migration、repository、既有 eval gate 定向测试，证明 draft rejected count 持久化、draft 不进入 membership/evaluator、secret redaction、approved-only 和 no-approved-cases 无回归。
- [x] 3.2 在 SQLite/PostgreSQL 运行同一 repository/UoW、tenant isolation、事务 rollback、experiment `(tenant_id, idempotency_key)`、request hash、evaluator profile/metric versions、create/decision 幂等与唯一约束 contract；验证新库直达 0011、已有 0009 数据经 0010/0011 原地升级、已有 0010 service volume 前滚、空库 downgrade 成功、任一 Phase 12.5 表非空时 downgrade 拒绝且 revision/evidence 不变，并记录 service smoke 命令和结果。
