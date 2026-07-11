## Source Links

- Product-Spec.md: REQ-016「Eval Gate 与 trace/eval 闭环」中的 behavior tags、optimization / holdout 和人工验收前置条件。
- DEV-PLAN.md: Phase 12.5「Eval Experiment 与 Harness Hill-Climb 闭环」的进入条件、交付内容与验收标准。
- API-Contract.md: EVL-004 的 create request、标签来源、split 与持久化响应字段。
- Design-Brief.md or design artifact: 不涉及 UI，无设计稿依赖。
- CONTEXT.md / ADR: 当前仓库没有适用于本变更的领域上下文或 ADR。

## Why

Phase 12 已提供真实示例 Agent 和 approved eval cases，但现有 dataset 仍是不可分层的单一集合，无法区分用于优化的已知样本与用于验收泛化能力的保留样本。必须先建立严格的行为标签、合格性过滤和可追踪 split，后续 baseline/candidate 对比才不会把 draft、secret 或无标签 case 混入评分并制造过拟合结论。

## What Changes

- 为 approved eval case 定义可查询、过滤和汇总的 behavior tag metadata，初始覆盖 `tool_selection`、`retrieval_quality`、`followup_quality`、`policy_approval`、`context_trust_boundary`。
- 建立 split 合格性门禁：只接收 approved、已通过 secret 检查且具备必需标签的 case；不合格输入 fail closed 并返回稳定诊断。
- 建立 optimization、holdout、regression subset 的确定性拆分、成员关系和租户隔离持久化契约。
- 建立 Phase 12.5 后续 experiment 与 acceptance 所需的 provider-neutral 持久化基础，统一关联 tenant、agent、dataset、request 和 evidence refs。
- 通过公共模块接口、SQLite/PostgreSQL migration contract 和存储集成测试证明数据形状与隔离边界。

## Non-Goals

- 不执行 baseline/candidate harness，不计算 score delta 或 acceptance recommendation。
- 不增加 EVL-004 HTTP route、CLI 命令或人工 accept 操作。
- 不改变 draft -> approve 的人工审核基础语义，不允许 detector 自动写 approved dataset。
- 不涉及 Phase 13 API/worker 分进程或 Phase 15 release automation。

## Capabilities

### New Capabilities

- `eval-dataset-splits`: 定义 behavior tags、case 合格性、optimization/holdout/regression split、租户隔离和持久化 evidence。

### Modified Capabilities

- 无。

## Impact

- 核心包：`agent_harness.evals` 新增 dataset/split 公共 DTO 与服务 seam。
- 存储：ORM、repository/UoW 和 Phase 12.5 migration 新增 split、experiment、acceptance 所需表结构，保持 SQLite/PostgreSQL 一致。
- 测试：新增公共模块、持久化、迁移、secret/标签合格性和 tenant isolation contract tests。
- 下游依赖：`eval-harness-experiment-comparison` 和 `eval-experiment-api-acceptance` 只能消费本变更产出的稳定 DTO 与存储边界。
