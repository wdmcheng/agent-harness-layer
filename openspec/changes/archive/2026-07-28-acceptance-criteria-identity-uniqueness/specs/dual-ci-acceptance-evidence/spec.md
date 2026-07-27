## MODIFIED Requirements

### Requirement: 需求验收矩阵完整覆盖显式选择的需求
系统 SHALL 由需求验收矩阵显式选择需要持续保障的 Product-Spec REQ。validator MUST 在按 REQ 分组或读取矩阵 evidence 前，要求 Product-Spec 中每个 live AC identity 在全部 REQ 范围内全局唯一；同一 identity 跨 REQ 或在同一 REQ 重复时 MUST fail closed 并报告重复 identity。每个所选 REQ 及其全部 AC MUST 唯一映射到仓库内存在的具体生产文件、实际执行该验收行为的一个或多个 CI job、至少一个以 `path.py::test_name` 或 `path.py::TestClass::test_name` 表示且真实验证该行为的精确 pytest node，以及各 job 产生的实际 evidence path。validator MUST 拒绝没有父 REQ 的孤立 AC，并且不得根据开发阶段或优先级标签推断验收范围。它还必须阻止遗漏、重复、文件级测试映射、共享 helper 或仅含 `pass`/`assert True` 的空壳冒充测试、producer 错配、泛化目录或虚假完成状态。required producer/test policy MUST 为受约束的每个 live identity 保存独立语义，不能因重复字典键让后一项行为覆盖前一项。复合验收使用等长的 `<br>` 分隔 job/evidence 列表，不得用其中任一绿色 gate 代替其余行为。live identity 迁移 MUST 在 Product Spec changelog 中追加旧 identity、行为限定和新 identity 的追溯，历史归档材料保持不可变。GitHub/GitLab MUST 各自包含独立的 required `acceptance-validate` 终端 job，下载或继承矩阵声明的全部 producer evidence，包括 clean runner 默认不会拥有的 `install`、`integration` 与 `build` evidence，并让后续 promotion 等待该门禁；`test-aggregate` 内部仅在已有证据时执行的自检不得代替该 job。

#### Scenario: 需求验收 validator 是 hosted required terminal gate
- **WHEN** GitHub 或 GitLab 在 clean runner 完成矩阵所需的 install、quality、test、integration、eval、smoke、license、build、release dry-run 与 CI contract producers
- **THEN** 独立 `acceptance-validate` job 消费全部对应 `ci-result/v1` 后才允许 pipeline 成功，GitLab promotion 显式等待该 job，任一 producer evidence 缺失或 identity 漂移均阻断后续发布

#### Scenario: Product Spec AC identity 全局唯一
- **WHEN** matrix validator 读取的 Product-Spec 在同一 REQ 或不同 REQ 中重复声明任一 AC identity
- **THEN** validator 在按 REQ 折叠集合、读取矩阵行或消费 evidence 前非零失败，并报告全部重复 identity

#### Scenario: acceptance matrix 完整
- **WHEN** matrix validator 读取 Product-Spec 与验收矩阵
- **THEN** Product-Spec 的 live AC identity 全局唯一，矩阵显式选择的每个 REQ 及其全部 AC 均有状态、存在的具体生产文件、CI job、存在且非空壳的精确 pytest node 和 evidence，且未验证边界不会被标成通过

#### Scenario: 矩阵范围不依赖阶段标签
- **WHEN** Product-Spec 的 REQ 没有开发阶段或优先级标签，或者标签后续发生变化
- **THEN** validator 只按矩阵中显式列出的 REQ 决定持续验收范围，并要求该 REQ 的全部 AC 同时存在

#### Scenario: 孤立 AC 不能扩张验收范围
- **WHEN** 矩阵列出某个 AC 但没有列出它所属的 REQ
- **THEN** matrix validator 非零失败并指出必须先显式选择父 REQ

#### Scenario: 泛化目录映射不能冒充追踪关系
- **WHEN** 任一矩阵 REQ/AC 的生产映射或测试映射只指向目录、路径不存在或越出仓库
- **THEN** matrix validator 非零失败并指出对应 ID 与字段

#### Scenario: 文件、共享 helper 或空壳节点不能冒充验收测试
- **WHEN** 测试映射只指向 Python 文件、精确 node 不存在，或目标 node 只含 `pass`/`assert True`
- **THEN** matrix validator 非零失败并指出该 ID 缺少可收集且非空壳的精确 pytest node

#### Scenario: CI 与 evidence producer 必须匹配实际验收行为
- **WHEN** 矩阵条目要求真实 service smoke、eval 或 quality/test 复合行为，却映射到没有执行全部行为的 gate/evidence，或者 `ci-result/v1.command` 不是该 gate 的 allowlisted Make target
- **THEN** matrix 必须拒绝该追踪结论；`AC-001` 要求执行 `uv sync --frozen` 的 `install`，`AC-002` 要求执行 `uv build` 的 `build`，`AC-003`/`AC-006` 要求 `integration` 并映射到 wheel 在 workspace 外安装、复制模板启动的集成测试；`AC-004`/`AC-061` 绑定实际 import 扫描，`AC-005` 绑定真实 fake adapter，`AC-019` 绑定 run/session/trace/eval 默认 tenant，`AC-023` 绑定 deny 零副作用及 audit，`AC-026` 绑定 MCP allowlist 拒绝，`AC-029`/`AC-052` 绑定无真实 key 的 fake-model eval，`AC-062` 分别绑定 API、worker、tool/model adapter 与 CanonicalEvent 关联交换；`AC-007`/`AC-011`/`AC-012`/`AC-060`/`AC-068` 同时要求 `test-aggregate` 的精确合同节点与执行真实服务行为的 `smoke-service`，其中 `AC-012`/`AC-068` 的 SQLite 节点与 PostgreSQL producer 必须分别列出；`AC-050` 要求独立终态 `acceptance-validate` 而不能由 `test-aggregate` 替代，`AC-051` 同时要求 `quality-aggregate`/`test-aggregate`，`AC-052` 要求 `eval`，`AC-053`/`AC-054` 同时要求 `quality-aggregate`/`eval`/`smoke-local`/`smoke-service`，`AC-065` 的完整 local fake run 时延要求 `smoke-local` 并映射到从公开入口完成 single-agent run 的正向节点；dependency lock `AC-070` 必须保留 lock/install/license/release-dry-run/test-aggregate 与 dependency policy 精确节点，API docs `AC-089` 必须独立保留 test-aggregate 与 typed profile/API docs 关闭精确节点；`acceptance-validate` 生成自身 result 时只允许跳过尚未落盘的 AC-050 自身 evidence 内容校验，映射和 producer 约束仍必须先通过

#### Scenario: identity 迁移保留行为与历史追溯
- **WHEN** 后加入的 API docs 行为从冲突的 `AC-070` 迁移到 `AC-089`
- **THEN** dependency lock `AC-070` 与 API docs `AC-089` 分别保留正确 production/test/CI/evidence 映射，Product Spec changelog 追加可判别的旧到新 identity 记录，历史 OpenSpec 归档材料不被改写

#### Scenario: 状态文档只按证据更新
- **WHEN** 当前维护目标的本地验证完成，且 frozen review 完成或 owner 的一次性审查豁免已明确记录
- **THEN** Product-Spec、changelog、DEV-PLAN、README 和 release 文档只声明已实现及本地 ready-to-archive；采用豁免时明确写为 `owner-waived`，不声明 reviewer PASS、hosted PASS、已归档或已发布
