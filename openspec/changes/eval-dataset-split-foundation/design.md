## Context

现有 `EvalCaseModel.metadata_json` 已能承载 provider-neutral metadata，`ReviewDatasetAdapter` 和 eval repository 已固定 draft/approved、tenant 与 secret 边界。Phase 12.5 需要在不破坏基础链路的前提下增加可复现 split，并为两个下游 change 提供统一持久化 schema。三个 change 的 DAG、共享验收和文件所有权见 `../phase-12-5-change-matrix.md`。

## Goals / Non-Goals

**Goals:**
- 提供可序列化、可验证的 behavior tag、split request/record 和 subset membership DTO。
- 用确定性多标签分层分配避免 optimization/holdout 随运行漂移，并在可行时保留各标签的 holdout representation。
- 以 `0009` 建立 split、experiment、harness acceptance 三类持久化记录；后续以追加式 `0010` 增加私有 claim，再以 `0011` 收敛 legacy `created`，禁止改写任何已应用 migration。
- 保持 SQLite/PostgreSQL、tenant isolation、large-payload refs 和 existing secret guard 一致。

**Non-Goals:**
- 不运行 evaluator、不计算 comparison，也不暴露 EVL-004 API/CLI。
- 不新增外部 dataset/splitting 依赖，不改变 approved 写入流程。

## Decisions

1. **标签继续保存在 `EvalCaseModel.metadata_json`，公共 seam 统一解析 `behavior_tags`。** 这样旧表无需回填；读取时由 `BehaviorTag`/DTO 规范化、去重并拒绝未知值。备选是为 case-tag 新建关联表，但 Phase 12.5 的样本规模与已有 JSON metadata 不值得增加第四张表和迁移复杂度。
2. **Regression 先预留，optimization/holdout 再做确定性多标签分层。** `regression_policy` 可引用固定 case ids 或 `regression=true` metadata；余下 cases 以 tenant/agent/dataset/case id 的 SHA-256 作为稳定 tie-break，并在最多五个闭集标签的有限状态空间内搜索同时满足 holdout/optimization 双侧标签覆盖和目标 case count 的首个稳定解。状态只记录已选数量、双侧标签 bitmask 和 membership bitset，不复制 payload；这样能回溯贪心局部最优，同时把状态数约束在 `target_size * 2^(5*2)` 量级。备选纯随机 split 会漂移，纯全局 hash 无法在小数据集保留标签分布，按 quota 的单向贪心则会在存在有效切分时误报不可表示。
3. **`0009` 建三表，`0010` 追加 execution claim，`0011` 收敛旧状态。** `eval_dataset_splits` 保存策略和 membership JSON；`eval_experiments` 在 0009 显式保存 `tenant_id`、`idempotency_key`、规范化 `request_hash`、evaluator profile、metric versions、baseline/candidate refs、score/comparison 与 provider status，并以 `(tenant_id, idempotency_key)` 唯一；`harness_acceptance_records` 保存每个 experiment 唯一且不可变的人工 review decision。第三张表以 `(tenant_id, experiment_id)` 唯一，记录 `decision_request_hash`、reviewer、reason、decision、nullable accepted version、`production_binding`、policy/audit/evidence refs；accepted 写 production binding，rejected 只保存幂等 decision/audit。由于 0009 已有可应用历史，私有 `execution_claim_id` / `execution_claim_expires_at` 由 `0010_eval_experiment_execution_claims` 以 nullable 列增量加入。0010 也已被本地 service profile 应用后才识别出 legacy created 风险，因此 `0011_eval_experiment_legacy_created_review` 只做追加式数据迁移：completed/failed evidence 原样保留，把旧版在 evaluator 前后都可能遗留的 `created` 原子转 `needs_review`。禁止回填旧 revision 或清空数据库掩盖升级问题。
4. **按 split、experiment、acceptance 拆分 Phase 12.5 repositories。** `eval_dataset_split_repositories.py`、`eval_experiment_repositories.py` 和 `eval_acceptance_repositories.py` 通过同一 UoW 暴露 provider-neutral seam，ORM 只留在 storage adapter。Experiment repository 额外提供原子 claim、续租、fencing 和 `needs_review` 转换；备选把所有方法塞进基础 `eval_repositories.py` 会让基础 gate 与实验闭环耦合并越过文件规模审查阈值。
5. **公共 split DTO 不返回 case payload。** membership 只含 case id、规范化标签和 refs；secret 校验失败只返回计数与字段路径。这样下游比较能定位 evidence，又不会复制 trace 大 payload。

## Affected Surfaces

- `packages/agent-harness/src/agent_harness/evals/datasets.py`：标签、请求/结果 DTO、split service 与稳定错误。
- `packages/agent-harness/src/agent_harness/storage/eval_experiment_models.py`、三个 Phase 12.5 repository、UoW composition：三类记录、执行 claim 与 tenant-scoped persistence。
- `packages/agent-harness/src/agent_harness/storage/migrations/versions/0009_eval_experiment_loop.py`、`0010_eval_experiment_execution_claims.py` 与 `0011_eval_experiment_legacy_created_review.py`：SQLite/PostgreSQL 基础 schema、claim/lease 列与 legacy 状态数据升级。
- `agent_harness.evals` / storage exports：只导出公共 DTO/protocol，不暴露 ORM。
- 无 UI、配置或外部 dependency 变化。

## Testing Seams

- 公共模块：tag normalization/filter 与 split service 输入输出。
- 持久化边界：repository/UoW create/get/update、跨 tenant 不可见、唯一约束、claim/续租/fencing 与不确定执行转 `needs_review`。
- Migration：SQLite 新库直达 head、已有 0009 terminal evidence 原地升级不变、legacy `created` 经 0011 转 `needs_review`、empty-only downgrade contract；PostgreSQL service smoke 验证已有 0010 volume -> 0011、三张表和约束。
- 基础回归：ReviewDatasetAdapter 的 draft/approved、secret redaction、`make eval` approved-only/no-approved-cases；真实 ExperimentService create 查询同 tenant/agent/dataset 的候选 case，由 split service 记录 draft rejected count，evaluator 仍只消费 approved membership。

## Risks / Trade-offs

- [旧 approved case 没有标签] → 不自动猜标签；split fail closed 并返回待整理 count，维护者先 curate metadata。
- [多标签小样本无法让每个标签同时覆盖 optimization/holdout] → 只在数学上可行时保证，结果记录 per-tag distribution 和不足诊断。
- [JSON membership 查询不如关联表高效] → P0 以可移植和审计简单为先；DTO/repository seam 允许未来在不改公共契约的情况下规范化。
- [三个 change 共用 schema] → 由 foundation change 统一维护 migration DAG；已应用 revision 不改写，必要字段通过新 revision 追加并重置联合审查。

## Migration Plan

升级时 `0009` 在现有 `0008` 后创建空的 Phase 12.5 表，不回填或改写现有 eval cases；`0010` 为 `eval_experiments` 追加两个 nullable 私有 claim 字段并保留已有状态/evidence；`0011` 保留 terminal evidence，把旧 `created` 原子转 `needs_review`。运行时若仍读到无 claim 的 `created` 也执行同一保守转换，不调用 evaluator/provider。应用回滚可保留新增空/已使用表并回滚代码；从 0011、0010 或 0009 downgrade 都只允许三张表全部为空的 disposable 环境，任一表非空时必须 fail closed、保持 revision 与 evidence 不变，生产环境采用 forward fix。

## Open Questions

无；标签集合、split 策略、表边界和下游依赖由上游真相源与联合矩阵确定。
