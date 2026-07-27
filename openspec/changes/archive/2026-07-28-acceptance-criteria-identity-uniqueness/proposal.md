## Source Links

- `Product-Spec.md`：REQ-003 的 API docs 关闭验收、REQ-023 的 dependency lock 验收、REQ-024 的可机械治理规则。
- `DEV-PLAN.md`：Phase 17 架构治理基线、Phase 17.1 前置治理修复、开发规则与验收矩阵风险。
- `docs/plans/architecture-evolution-plan.md`：Phase 17 独立治理修复、D-011、D-013、验证与 handoff 协议。
- `docs/plans/architecture-evolution-change-matrix.md`：Phase 17.1 依赖、共享验收、文件所有权和串行边界。
- `Design-Brief.md` 或设计稿：不适用；本 change 不改变 UI 或交互。
- `CONTEXT.md`：不适用；本 change 不形成新的领域术语或上下文边界。
- `docs/adr/0004-swagger-ui-offline-assets.zh-CN.md`、`docs/adr/0005-redoc-offline-assets.zh-CN.md`：API docs 启停与离线资源行为保真参考；本 change 只迁移验收 identity，不改变既有 ADR 决策。

## Why

live Product Spec 和验收矩阵把 API docs 关闭行为与 dependency lock 行为同时标成 `AC-070`；矩阵会拒绝重复行，而 policy 字典只能保留一套语义，导致验收 evidence 可能被覆盖。Phase 18 新增验收映射前必须先建立全局唯一、可追溯且可机械验证的 AC identity。

## What Changes

- 保留 Git 历史中先出现的 dependency lock `AC-070`，将后加入的 API docs 关闭验收迁移为未占用的 `AC-089`。
- 原子同步 live Product Spec、验收矩阵、required producer/test policy 和相关维护状态，使两项行为分别保持正确的 production、test、CI 与 evidence 映射。
- 扩展验收矩阵公开 CLI validator，使 Product Spec 中跨 REQ 的重复 AC identity 在矩阵解析和 evidence 校验前 fail closed。
- 增加先红后绿的公开 CLI 合同，覆盖 Product Spec 重复、矩阵重复、policy 映射错配、缺失 producer/test/evidence、两项行为保真和历史迁移追溯。
- 在 Product Spec changelog 中追加 `AC-070`（API docs）到 `AC-089` 的迁移说明；不改写历史 OpenSpec 归档材料。

## Non-Goals

- 不改变 API docs、dependency lock、CI evidence 或 release runtime 行为。
- 不重排 `AC-071` 至 `AC-088`，不批量改写其他验收 identity。
- 不修改 `openspec/changes/archive/`，不进入 Phase 18，不接入真实 provider。
- 除用户已明确授权的本次 scoped local 文档提交外，不执行其他 commit、push、主规格 sync、OpenSpec archive、发布或部署。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `dual-ci-acceptance-evidence`：把 Product Spec 的 AC identity 全局唯一性纳入验收矩阵 validator 的 fail-closed 合同，并保持每个 live identity 的 production/test/CI/evidence 语义映射唯一。

## Impact

- 规格与维护状态：`Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`DEV-PLAN.md`、`docs/acceptance-matrix.md`、架构演进 living plan 与 change matrix。
- 机械门禁：`scripts/acceptance_matrix.py`、`scripts/acceptance_matrix_policy.py`、现有 `acceptance-validate` CI seam。
- 合同测试：验收矩阵 validator、语义映射、evidence identity、dependency policy 与 API docs 既有行为测试；修订 `tests/contracts/test_dependency_version_policy_contracts.py` 中把全仓状态永久锁成“无 active change”的陈旧断言，使其只验证 Phase 16 uv change 已归档且不再 active。
- 公共 API、数据库、依赖、UI 和 runtime：无变化。
