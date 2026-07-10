## ADDED Requirements

### Requirement: Approved eval case 使用规范化行为标签
系统 SHALL 从 eval case metadata 读取规范化的 behavior tags，并支持按标签查询、过滤和汇总 approved cases。初始受支持标签 MUST 包含 `tool_selection`、`retrieval_quality`、`followup_quality`、`policy_approval`、`context_trust_boundary`；标签列表 MUST 去重并使用稳定顺序，未知标签 MUST 返回可操作的 validation error。

#### Scenario: 按行为标签过滤 approved cases
- **WHEN** 调用方在同一 tenant、agent 和 dataset 中按 `retrieval_quality` 查询 approved cases
- **THEN** 系统只返回 metadata 包含该标签的 approved cases，并返回该标签的准确 case count

#### Scenario: 未知标签被拒绝
- **WHEN** 调用方使用不在受支持集合中的标签创建或查询 split
- **THEN** 系统返回稳定 validation error，错误包含非法标签路径和允许值，且不创建 split 或其他持久化 side effect

### Requirement: Split 合格性门禁 fail closed
系统 SHALL 仅允许 `status="approved"`、通过现有 secret/隐私检查、具备至少一个受支持行为标签且归属请求 tenant/agent/dataset 的 case 进入 experiment split。draft、secret 命中、标签缺失、归属不匹配或不存在的 case MUST 被拒绝；拒绝结果 MUST 不包含原始 secret 或完整 payload。

#### Scenario: Draft case 不进入 split
- **WHEN** 候选数据集中同时存在 draft 和 approved cases
- **THEN** split 只使用 approved cases，并明确报告被拒绝的 draft count，draft case id 不出现在任何 subset membership

#### Scenario: Secret 命中 case 不进入 split
- **WHEN** approved case 的 payload 或 metadata 在 split 前的现有 secret 检查中命中 secret fixture
- **THEN** 系统拒绝创建 split，返回脱敏 validation summary，且不持久化包含该 case 的 membership 或原始 secret

#### Scenario: 跨 tenant case 不可见
- **WHEN** tenant A 请求使用 tenant B 的 approved case 创建或读取 split
- **THEN** 系统按资源不可见语义失败，且响应不泄漏 tenant B 的 case、dataset、标签或 count

### Requirement: Optimization、holdout 与 regression subset 可追踪且可复现
系统 SHALL 使用版本化 `split_strategy`、optimization/holdout ratio 和 `regression_policy` 创建互斥的 optimization、holdout、regression subsets。初始唯一策略 MUST 为 `deterministic_multilabel_v1`；默认 optimization/holdout ratios MUST 为 0.8/0.2，两个值 MUST 分别大于 0 且小于 1、总和在 `1e-9` 容差内等于 1。`regression_policy` MUST 包含可选 `case_ids`、`critical_case_ids`、`metadata_flag`（默认 `regression`）、`critical_tags` 和非负 `max_holdout_regression`；非法、重复、跨 tenant/agent/dataset 或非 approved case ref MUST fail closed。Regression cases MUST 先从完整合格集合中按 policy 预留，ratios 只对剩余 cases 计算；剩余 cases MUST 按行为标签进行确定性分配。相同合格输入和策略 MUST 产生相同 membership。optimization 与 holdout MUST 均非空，且每个具备足够样本的请求标签 MUST 在可行时同时出现在 optimization 和 holdout。

#### Scenario: 相同输入产生相同 split
- **WHEN** 调用方以相同 tenant、agent、dataset、tags、ratios、strategy、regression policy 和 approved case 集合重复计算 split
- **THEN** 两次结果的 optimization、holdout、regression case membership 完全一致

#### Scenario: Regression cases 与优化样本隔离
- **WHEN** `regression_policy` 选择已修复的命名 regression cases
- **THEN** 这些 cases 只进入 regression subset，不进入 optimization 或 holdout，后续 candidate comparison 必须单独报告其结果

#### Scenario: 无法形成非空 holdout 时失败
- **WHEN** 合格 cases 在预留 regression subset 后不足以形成非空 optimization 和 holdout
- **THEN** 系统返回稳定 split validation error，且不持久化空 holdout 或伪造 membership

#### Scenario: 非法策略、比例或 regression ref 无副作用
- **WHEN** `split_strategy` 未知、ratios 越界或不等于 1，或 `regression_policy` 引用不存在、跨归属或非 approved case
- **THEN** 系统返回带字段路径的稳定 validation/not-found error，不创建 split、experiment、decision、audit 或 provider side effect，也不泄漏不可见 case 信息

### Requirement: Split record 保留关联与最小 evidence
每个持久化 split SHALL 包含 `split_id`、`tenant_id`、`agent_id`、dataset、tags、strategy、ratios、regression policy、三个 subset membership/count、`request_id`、脱敏 evidence refs 和时间戳。公共 DTO MUST 不包含 eval case 完整 payload、secret、provider SDK object 或绝对本地路径；读取 MUST 受 tenant 隔离。

#### Scenario: Split record 可供 experiment 复用
- **WHEN** 同 tenant 的 experiment service 读取已持久化 split
- **THEN** 系统返回稳定 DTO，包含三个 subsets 的 case ids/count 与策略摘要，且每个 case 仍可追溯到原 approved dataset

#### Scenario: Split response 不内联大 payload
- **WHEN** split 中的 approved case 包含大输入、输出或 artifact
- **THEN** split DTO 只返回 case id、标签、摘要和 evidence/artifact refs，不返回完整 case payload 或 provider raw response
