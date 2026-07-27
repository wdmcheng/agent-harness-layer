## Context

Product Spec 的 dependency lock `AC-070` 在 `fa5ad90c` 先出现，API docs 同号验收在 `3aedac83` 后加入。当前验收矩阵有两行 `AC-070`，validator 会在矩阵层拒绝重复；但 `requirement_groups` 先把每个 REQ 的 AC 收进 `set`，无法发现 Product Spec 跨 REQ 重号，`acceptance_matrix_policy.py` 的字典也只能保留一套 `AC-070` producer/test 语义。Phase 17.1 必须在不改变两项业务行为、不重写归档历史的前提下闭合 live identity、policy 与 evidence 追踪。

## Goals / Non-Goals

**Goals:**

- 每个 live AC identity 在整个 Product Spec 中全局唯一，并由公开 validator CLI fail closed。
- 保留先出现的 dependency lock `AC-070`，把后出现的 API docs identity 迁移到未占用的 `AC-089`。
- 两项行为分别保持现有 production、test、CI 与 evidence 语义，迁移历史可从 live changelog 追溯。
- 用先红后绿合同覆盖重复、映射错配、缺项和正向仓库状态。

**Non-Goals:**

- 不修改 API docs 或 dependency/release 的运行行为。
- 不引入新的框架、设计模式、数据库迁移、配置、API 或 CI job。
- 不重排其他 AC，不修改历史 OpenSpec archive，不进入 Phase 18。

## Decisions

1. **以 Git 引入顺序裁决保留 identity。** dependency lock 的 `AC-070` 先出现并已绑定完整 release policy，故保留；API docs 使用下一个未占用的 `AC-089`。备选方案是后缀 `AC-070A` 或整体顺延，但前者继续让旧 `AC-070` 语义含混，后者会扩大 `AC-071` 至 `AC-088` 的迁移面。
2. **在分组前做全局计数。** validator 对 `AC_ID.findall(Product-Spec)` 先做全局 `Counter`，发现重复即报告全部 identity，再构造 REQ → AC 集合。备选方案是仅依赖矩阵重复检测，但这无法阻止 Product Spec 重号被集合吞掉，也无法保护尚未被矩阵选择的 REQ。
3. **沿用现有 CLI/Policy seam，不增加抽象层。** 变化轴只有 AC identity 与其静态语义映射；直接扩展现有 parser、policy 常量和 contract tests 比新增 registry/class 更符合 SRP、YAGNI 与最小 seam 原则。
4. **迁移记录追加到 live changelog，archive 保持不可变。** 新记录明确限定“API docs 的旧 `AC-070`”到 `AC-089`，避免与仍有效的 dependency `AC-070` 混淆。备选方案是修改历史 changelog/归档文件，会破坏已发生事实与审查追溯，因此拒绝。
5. **evidence 身份按当前 diff 重新生成。** identity 迁移会改变 Git 输入摘要；旧 `.artifacts/ci/*` 不得冒充当前证据，最终只接受由当前冻结 diff 产生的 producer results。

## Affected Surfaces

- Product/计划：`Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`DEV-PLAN.md`、架构演进 living plan 与 change matrix。
- 验收真相：`docs/acceptance-matrix.md`。
- 机械门禁：`scripts/acceptance_matrix.py` 的公开 CLI 与 `scripts/acceptance_matrix_policy.py` 的 required mappings；既有 Make/CI `acceptance-validate` 入口不变。
- 测试：验收矩阵 validator 与语义映射合同；既有 dependency policy、API docs、evidence identity 和文档合同作为回归证据；`test_dev_plan_reports_archived_uv_change` 的断言从“全仓必须无 active change”收窄为“Phase 16 uv change 已归档且自身不再 active”，避免历史 change 合同阻断合法后续 change。
- API、数据库、依赖、UI、配置、runtime、release 输出 schema：无变化。

## Testing Seams

- 公开 CLI：以临时 Product Spec/矩阵调用 `scripts/acceptance_matrix.py --spec ... --matrix ...`，断言跨 REQ/同 REQ 重号在读取 evidence 前稳定失败。
- Policy/矩阵合同：断言 `AC-070` 与 `AC-089` 分别要求正确的 producer 与精确 pytest nodes，删除或互换任一项都会失败。
- 当前仓库正例：在 current diff 对应 producer evidence 就绪后运行默认 validator，确认 live identity 与矩阵一一闭合。
- 行为回归：运行 dependency policy/lock 节点和 API docs typed profile/app surface 节点，证明迁移只改变 identity。
- 历史追溯：合同读取 live changelog 的限定迁移记录，并确认 change diff 不包含 `openspec/changes/archive/`。

## Risks / Trade-offs

- [Risk] `AC-089` 位于 REQ-003 而编号晚于相邻条目，阅读顺序不连续。→ 保留全局 append-only identity 比批量重编号更安全；changelog 解释迁移原因。
- [Risk] 只更新 Product Spec/矩阵而漏掉 policy，会让 API docs 映射失去强制语义。→ 为两个 identity 建立独立 required producer/test 合同和正反例。
- [Risk] 旧 evidence 因输入摘要变化全部过期，直接运行 validator 会先报 identity drift。→ 按 tasks 中的 producer 顺序重新生成当前 diff 的 evidence，再运行终态 validator。
- [Risk] 全局计数误把普通正文中的历史提及当 live identity。→ 继续只匹配 Product Spec 验收列表的 `- [ ] AC-*:` / `- [x] AC-*:` 语法，不扫描叙述文本。

## Migration Plan

1. 先加入会在当前实现上失败的 validator/policy/追溯合同并保存红灯证据；同时保存 `test_dev_plan_reports_archived_uv_change` 因陈旧全局 active 状态断言产生的现有红灯。
2. 原子迁移 live API docs identity 与矩阵/policy 映射，扩展 Product Spec 全局唯一性校验。
3. 追加 changelog 迁移记录并同步 DEV-PLAN、living plan/change matrix 状态；不改 archive。
4. 运行聚焦合同、既有行为回归、quality、全量 test、文档合同和当前 diff 的 evidence/validator。
5. 回滚时整体撤销本 change 的 live 文档、policy、parser 与测试 diff；不存在数据或 runtime migration。

## Open Questions

无。identity 保留顺序、目标编号、文件所有权和非目标均由当前 Git 与 Phase 17 计划冻结。
