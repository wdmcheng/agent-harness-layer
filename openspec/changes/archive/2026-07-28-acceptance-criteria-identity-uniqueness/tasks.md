## 1. 公开 CLI 红灯合同

- [x] 1.1 通过 `scripts/acceptance_matrix.py --spec ... --matrix ...` 的公开 CLI 新增 Product Spec 同 REQ/跨 REQ 重复 AC identity 合同；修改 parser 前精确运行，两项均因 validator 错误返回 `0` 而红，证明当前会吞掉重号。
- [x] 1.2 新增 `AC-070` dependency lock 与 `AC-089` API docs 的 required producer/test policy、当前矩阵语义和 changelog 迁移追溯合同；未迁移 live 文件上 3 项均红，分别证明 `AC-089` producer/test policy 缺失和 live identity 尚未迁移。
- [x] 1.3 运行 `tests/contracts/test_dependency_version_policy_contracts.py::test_dev_plan_reports_archived_uv_change`，保存陈旧“全仓无 active change”断言的退出码 `1` 红灯；随后把合同收窄到 `relax-release-uv-patch-range` 自身已归档且不再 active，精确节点已绿。

## 2. Identity 与机械门禁实现

- [x] 2.1 在 `requirement_groups` 按 REQ 折叠前统计 Product Spec 全部 live AC identity，重复时由 validator CLI 稳定报告全部冲突；同 REQ/跨 REQ 公开 CLI 合同已绿，既有矩阵重复语义保留。
- [x] 2.2 保留 dependency lock `AC-070`，将 live API docs 验收原子迁移为 `AC-089`；已同步矩阵与 required producer/test policy，错 producer、错测试节点和 live 正向合同均绿。
- [x] 2.3 在 `Product-Spec-CHANGELOG.md` 追加带行为限定的 `AC-070`（API docs）→ `AC-089` 追溯；`git diff --name-only -- openspec/changes/archive` 无输出，dependency/API docs 生产实现未修改。

## 3. 维护状态与计划同步

- [x] 3.1 同步 `DEV-PLAN.md`、架构演进 living plan 与 change matrix，分开记录实现、验证、fresh review、未闭合 evidence 和未归档状态；保留 Phase 18 未授权，唯一下一动作是复用证据做 fresh 实现 review。

## 4. 当前冻结内容验证

- [x] 4.1 运行验收矩阵 validator、语义映射、evidence identity、dependency policy、API docs 和文档合同的聚焦 pytest，退出码 `0`；后续 `AC-065/066` 归属修订的 validator 合同同样 PASS。
- [x] 4.2 对实现冻结内容运行 `make quality`、`make test`、所需 evidence producers、`uv run python scripts/acceptance_matrix.py`、owned-file `git diff --check` 和 `openspec validate acceptance-criteria-identity-uniqueness --type change --strict`；全部退出码为 `0`，验收矩阵为 `98/98`，全量测试为 `1352 passed, 223 skipped`。其后只同步完成状态，owner 明确裁决不为状态记录重跑 evidence。
