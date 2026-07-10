## 1. 标签与 Split 公共 Seam

- [x] 1.1 先通过 `agent_harness.evals` 公共模块测试锁定 behavior tag 规范化、approved/secret/tenant 合格性和稳定错误，再实现 tag 查询、过滤与 fail-closed eligibility。
- [x] 1.2 先通过公共 split service 测试锁定 `deterministic_multilabel_v1`、0.8/0.2 默认值、ratio 范围/求和、regression policy 字段与非法/跨归属 ref、regression 预留、确定性多标签 optimization/holdout 分配、非空门禁和可复现 membership，再实现 `datasets.py` DTO/service。

## 2. Phase 12.5 持久化基础

- [ ] 2.1 先通过 SQLite migration/model contract 锁定 `eval_dataset_splits`、`eval_experiments`、`harness_acceptance_records` 的字段，尤其是 experiment 的 `(tenant_id, idempotency_key)` 唯一约束、request hash、evaluator profile、metric versions，以及 decision request hash、nullable accepted version、production binding 和 `0008 -> 0009` 链，再实现 ORM 与 `0009_eval_experiment_loop.py`。
- [ ] 2.2 先通过 repository/UoW 集成测试锁定 split/experiment create/get/update、每个 experiment 唯一不可变 accepted/rejected decision、同 reviewer 同 body 幂等、跨 reviewer/decision/version 冲突、原子 audit、tenant isolation 和无完整 payload DTO，再实现独立 experiment repository 与 composition。

## 3. 集成与回归证据

- [ ] 3.1 导出新的公共 DTO/protocol，运行 tag/split、SQLite migration、repository、既有 eval gate 定向测试，证明 draft/approve、secret redaction、approved-only 和 no-approved-cases 无回归。
- [ ] 3.2 在 SQLite/PostgreSQL 运行同一 repository/UoW、tenant isolation、事务 rollback、experiment `(tenant_id, idempotency_key)`、request hash、evaluator profile/metric versions、create/decision 幂等与唯一约束 contract；验证空库 downgrade 成功、任一 Phase 12.5 表非空时 downgrade 拒绝且 revision/evidence 不变，并记录 service smoke 命令和结果。
