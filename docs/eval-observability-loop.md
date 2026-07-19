# Eval 与 Observability 闭环

适用读者：维护 trace-to-eval、experiment、provider fan-out 和人工 acceptance 的 app developer 与 scaffold maintainer。

导航：[根 README](../README.md) · [扩展指南](extension-guide.md) · [Adapter 合同](adapter-contracts.md) · [Context 与信任边界](context-and-trust-boundary.md) · [安全策略](security-policy.md) · [Release 边界](release-process.md)

本文说明 Phase 11 approved-only 基础链路与 Phase 12.5 experiment 闭环，包括 case 准入、标签、split、harness manifest、comparison、人工 acceptance 和 provider 降级的真实运行边界。当前实现保持 provider-neutral；provider adapter 和 evaluator 都不能绕过本地证据、policy 或人工批准。

## 基础链路保持不变

```text
failed / low-score trace
  -> draft eval case
  -> 人工 review
  -> approved dataset
  -> eval run
  -> score sink
  -> local/jsonl 和可选 provider evidence
```

自动 detector 只能写 draft，approved eval case 必须经过人工 review。`make eval` 仍只运行 approved cases；approved dataset 为空时返回稳定 `no-approved-cases`，不伪造分数。Experiment 是这条链路之后的只增不改能力，不允许 draft 绕过 review。

## Phase 12.5 experiment 闭环

```text
approved eval cases
  -> behavior tags 与安全门禁
  -> optimization / holdout / regression split
  -> baseline harness evaluation
  -> candidate harness evaluation（可选）
  -> per-tag / holdout / failure comparison
  -> 人工 reviewer + policy
  -> immutable accepted/rejected decision + audit
```

Optimization subset 用于观察目标行为改善；holdout 和 regression 是防止过拟合的独立门禁。总分上涨不是接受条件。省略 candidate 只创建不可变 baseline snapshot，不能在原 experiment 上后补 candidate。

## Case 准入与 curation

### 三类来源

- 手写 case：只用于明确、可复现的行为边界。输入、期望和标签必须由 reviewer 逐项确认，不得把实现细节写成唯一正确答案。
- 生产 trace：先经过 secret/隐私脱敏和质量筛选，再进入 draft queue；失败、低分或人工标记只能触发 draft，不能自动 approved。
- 外部数据集：必须记录来源许可、转换规则和逻辑 evidence ref；导入后仍走本项目人工 review，不因上游标签而自动可信。

所有来源在进入 split 前都必须同时满足：当前 tenant/agent/dataset 可见、状态为 `approved`、`metadata.behavior_tags` 非空且属于封闭枚举、payload/metadata 不含 secret 或 `[REDACTED]` 标记、evidence ref 不是本机绝对路径。任一条件失败即 fail closed。

### 行为标签

初始封闭标签为：

- `tool_selection`
- `retrieval_quality`
- `followup_quality`
- `policy_approval`
- `context_trust_boundary`

标签写在 case 的 `metadata.behavior_tags`，不是文件名或自由文本注释。同一 case 可以有多个标签；comparison 使用 split 持久化的 `case_tags` 作为权威映射，不信任 evaluator 临时返回的标签。

### 清理标准

定期按 tenant、agent、dataset、tag 检查：

- 饱和：连续多个 harness version 都稳定满分、且不再区分候选行为的 case，应降为低频 regression 或替换成更有判别力的边界 case。
- 重复：语义、期望、failure mode 和 evidence 都等价的 case 只保留一个权威版本，避免某一标签被重复样本放大。
- 失真：生产行为、工具契约或 policy 已变化，导致期望不再成立的 case 必须回到 draft 重新 review，不得直接改 approved payload。
- 污染：发现 secret、绝对路径、provider raw response 或来源许可不明时立即移出候选集合并走安全处置；不要只删除显示字段后继续使用。

## Split 与 regression policy

`deterministic_multilabel_v1` 是当前唯一策略。相同请求和相同 eligible membership 产生同一 `split_id`；optimization 与 holdout 均必须非空。显式 `case_ids`、`critical_case_ids` 或命中 `metadata_flag` 的 case 进入 regression subset，不参与 optimization/holdout 抽样。

`RegressionPolicy` 的关键语义：

- `max_holdout_regression` 是允许的 aggregate score 绝对下降；`candidate - baseline` 小于其负值即失败。
- `critical_case_ids` 与命中 `critical_tags` 的 regression cases 必须通过。
- `case_ids` 用于命名 failure mode；candidate 修复其中任一项可作为目标改善依据，但不能覆盖 holdout 或关键 regression 失败。

Split 只持久化 case ID、权威标签、分布、拒绝计数和安全 refs，不复制完整 case payload。

## Harness version manifest

Manifest 必须精确覆盖六类会改变 agent 行为的输入：

- `prompt_instruction`
- `tool_descriptions`
- `agent_config`
- `retrieval_config`
- `policy_defaults`
- `model_adapter_settings`

构建器规范化 mapping/list 后计算每类 checksum 和整体 `version_id`。持久化 manifest 只保留 checksum、脱敏 diff summary 和逻辑 evidence ref，不保留原文、SDK object、secret 或绝对路径。Accepted record 只建立版本与 experiment evidence 的生产候选绑定，不自动改写 prompt、tool description 或配置文件。

模板默认 `RecordedApprovedCaseEvaluator` 只能读取 approved case 的本地证据：优先使用 `metadata.experiment_scores[version_id][metric]`，`exact_match` 缺省时比较 payload 的 `output` 与 `expected`。它不会从 checksum 反推或执行生产配置。需要真实 harness executor 时，通过 `ExperimentEvaluator` protocol 注入 adapter，仍必须返回相同 split、profile、metric version 和安全 refs。

Evaluator 成功结果也不被默认信任。每个 case/local evidence ref 都必须通过统一 secret、本机绝对路径、单项长度、列表数量和聚合大小门禁；DTO 构造后被 adapter 变异也会在 service 边界重新校验。各列表合法但 baseline/candidate/comparison 顶层或同一 case 的 failure diff 合并后超过公共 100 项或 16 KiB 时，DTO 构造与 terminal 写入前只保留 `db://eval-experiments/<id>` 真相引用，完整 refs 留在本地 score summary，create/show/compare/replay、CLI 与 provider payload 共用同一有界结果。非法输入失败时 experiment 记录 `eval.experiment.evidence_invalid` 的有界摘要，不把原始 ref 写入公共响应或 provider payload。

## Comparison 与 provider 降级

Baseline 和 candidate 必须使用同一 split、evaluator profile 与 metric versions。Comparison 返回 per-tag baseline/candidate/delta、holdout delta、regressions、new/fixed failures、非空封闭 reason codes 和 recommendation。

`accept` 只表示算法建议，`reject` 或 `needs_review` 都不能被入口层覆盖。Failure 明细超过 inline 上限时，只在响应中保留截断摘要和 `failure_details_ref`，完整内容仍在本地 experiment score summary。

Local DB evidence 先提交，可选 provider 后 fan-out。Provider 写入失败只把状态改为 `*_with_degradation` 并返回脱敏摘要；不得删除 experiment/comparison、泄漏 provider raw response/credential，或把 local evidence 失败伪装成 provider degradation。

### 执行 claim、重放与 `needs_review`

创建时，split、experiment 和首个私有 execution claim 在同一事务提交，避免并发同 key 产生 orphan split。协调器用 claim id 做结果写入 fencing，并在 evaluator 运行期间续租：

- 有效 claim 下的同 key/body 重放只返回同一 `experiment_id` 与 `running`，不会再次调用 evaluator/provider。
- heartbeat 续租返回失败或抛出异常即视为 claim 丢失；协调器停止可信终态写入，repository 也会原子拒绝 owner 匹配但租约已过期的续租与结果提交。
- 确定性 evaluator failure 写 `failed`，错误只保留封闭 code、有界通用摘要和安全 evidence refs；原始异常、provider raw response 与大 payload 不入库。
- 进程中断、claim 过期，或 evaluator 已返回但 terminal 写入失败时，系统无法证明外部副作用是否完成，因此转为 `needs_review` 并清除私有 claim。后续重放只返回该状态，不自动重跑。
- 旧 `0009` 曾在 evaluator 前提交 `created`，因此升级或重放看到无 claim 的 legacy `created` 时也必须转 `needs_review`；它不能被解释成“肯定尚未执行”。
- `needs_review` 不是 provider degraded。维护者必须对照 experiment id、split refs 和外部 evaluator evidence 人工判断；当前 Phase 12.5 不提供“强制重跑”入口，也不自动修改 harness 或生产配置。

## 人工 acceptance

Accepted decision 依次要求：

1. comparison 完整且 recommendation 为 `accept`；
2. `accepted_harness_version` 精确等于已比较 candidate；
3. 认证 identity 提供 reviewer/tenant，body 不能覆盖；
4. `eval.harness.accept` policy 返回 allow；deny 为 403，`require_approval` 为 409，且不隐式创建嵌套 approval；
5. 唯一 immutable decision、production binding 和 decision audit 在同一 UoW/savepoint 内提交。

Rejected decision 仍记录 reviewer、reason、policy、evidence 和 audit，但 `accepted_harness_version` 与 production binding 必须为空。同 reviewer、同规范化 body 重试返回同一 decision，不重复 audit；其他 reviewer 或不同 body/version 返回 409。

## HTTP 与 CLI 操作

HTTP 使用 EVL-004 四个路径：

- `POST /api/v1/evals/experiments`，要求 `Idempotency-Key`；新建 201，安全重放 200。
- `GET /api/v1/evals/experiments/{experiment_id}`。
- `GET /api/v1/evals/experiments/{experiment_id}/comparison`。
- `POST /api/v1/evals/experiments/{experiment_id}/accept`。

Service profile 使用 HTTPBearer；tenant 来自 identity。读取其他 tenant 与不存在统一返回 404。Create/read 分别要求 `eval.experiment.create` / `eval.experiment.read` 或 `*` 权限；accept 由 policy seam 判定。

CLI 使用同一 service/DTO/persistence。以下 local 命令必须从 service-app 根目录运行，并先完成 [`templates/service-app` Quick Start](../templates/service-app/README.md#quick-start) 的 fingerprint key、`STORAGE_DSN` 与 SQLite migration；`experiment.json` 必须按下文 DTO 说明准备，`EXPERIMENT_ID` 和 `CANDIDATE_VERSION` 分别取自 create 结果和 candidate manifest：

```bash
uv run agent-harness eval experiment create \
  --request-file experiment.json \
  --idempotency-key harness-candidate-2026-07-11 \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness eval experiment show "$EXPERIMENT_ID" \
  --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN"
uv run agent-harness eval experiment compare "$EXPERIMENT_ID" \
  --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN"
uv run agent-harness eval experiment accept "$EXPERIMENT_ID" \
  --decision accepted \
  --reason "人工核对目标标签、holdout 和 regression 后通过" \
  --accepted-harness-version "$CANDIDATE_VERSION" \
  --reviewer local-reviewer \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"
```

`experiment.json` 对应 `EvalExperimentCreateRequest`，包含 agent、dataset、tags、split strategy、baseline manifest 和可选 candidate manifest；不要放 tenant、reviewer、secret 或 provider object。四个命令成功时输出单个稳定 JSON object，失败输出带 `code`、`message`、`request_id` 的脱敏 error object 并以非零状态退出。

## 控制文档与变更边界

Phase 12.5 的真相源是：

- `Product-Spec.md` REQ-016
- `API-Contract.md` EVL-004
- `DEV-PLAN.md` Phase 12.5
- 本文的操作和维护边界

本阶段按 `foundation -> comparison -> API acceptance` 的依赖顺序交付，并于 2026-07-11 归档为 `2026-07-11-eval-dataset-split-foundation`、`2026-07-11-eval-harness-experiment-comparison`、`2026-07-11-eval-experiment-api-acceptance`。对应 main specs 已同步；后续维护不得把 Phase 13 API/worker split 或 Phase 15 release automation 混回本闭环。

## 公开 seam、验证与排障

公开扩展点是 `ApprovedCaseExecutor`、`ExperimentEvaluator`、`ExperimentEvidencePublisher`、case/dataset/experiment repository、score sink、`TelemetryFacade` 与 `ProviderTelemetryAdapter`。新增 evaluator/provider 时保持 DTO、split、metric version、local-first persistence、redaction 和 degradation 语义，不让 SDK object 或 raw response 越界。

```bash
make eval        # 只运行 approved cases
make test        # eval、experiment、provider 与恢复合同
make smoke-local # fake model + local JSONL evidence
# 真实 API/worker/PostgreSQL/Redis 组合才使用：
make smoke-service
```

证据入口包括 `tests/contracts/test_eval_gate_trace_loop_contracts.py`、`tests/contracts/test_eval_execution_contracts.py`、`tests/contracts/test_eval_experiment_api_contracts.py`、`tests/contracts/test_eval_experiment_evidence_boundaries_contracts.py`、`tests/contracts/test_observability_local_first_fanout_contracts.py` 和 `templates/service-app/eval-cases/`。

常见故障：`no-approved-cases` 表示没有可执行的已审核样本；provider degraded 时先确认 local score/event 已提交；comparison 不接受时检查 holdout、critical regression、metric version 和安全 refs，而非只看总分；`needs_review` 表示执行副作用无法被证明，必须人工核对 claim 与外部 evidence，当前没有强制重跑入口。Phase 15 才负责把这些证据接入自动 release gate；本 checkout 没有该自动化。
