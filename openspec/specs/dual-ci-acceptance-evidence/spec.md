# dual-ci-acceptance-evidence Specification

## Purpose
定义 GitHub Actions 与 GitLab CI 的等价质量门禁、可追溯证据、受保护发布流程，以及由需求验收矩阵显式选择的长期 REQ/AC 覆盖契约。
## Requirements
### Requirement: 两套 CI 使用等价稳定入口
GitHub Actions 与 GitLab CI SHALL 调用同一组仓库 Make targets，等价覆盖 lock/install、ruff format、ruff lint、pyright、import boundary、unit/contract、integration、eval、smoke-local、真实 PostgreSQL/Redis smoke-service、build、license 和 release dry-run。

两套 pipeline MUST 分别以独立聚合 job 真实执行 `make quality` 与 `make test`；聚合 job与细粒度 quality/test jobs 都是 required，任一方不能作为另一方的替代证据。

#### Scenario: GitHub push 或 pull request 触发完整门禁
- **WHEN** GitHub CI 收到受支持的 push、pull request 或手动触发
- **THEN** pipeline 创建全部 required jobs，且每个业务命令来自仓库稳定入口

#### Scenario: GitLab push 或 merge request 触发等价门禁
- **WHEN** GitLab 收到对应 push、merge request 或 web pipeline
- **THEN** pipeline 的 required job、Make target、依赖和 artifact 集合与 GitHub 等价

#### Scenario: 两个聚合命令在两套 CI 中实际执行
- **WHEN** GitHub 或 GitLab required pipeline 运行
- **THEN** `quality-aggregate` 与 `test-aggregate` 分别调用 `make quality` 与 `make test` 并保存独立 result/log，同时细粒度 jobs 继续单独执行

### Requirement: 质量与测试结果独立可归因
系统 MUST 为 ruff format、ruff lint、pyright、import boundary、unit/contract 和 integration 生成独立 job 结果；聚合 `make quality` 或 `make test` 通过不能代替缺失子项。

#### Scenario: 单个质量项失败阻断下游
- **WHEN** 任一 required quality 或 test job 返回非零
- **THEN** 该 job 的失败结果和日志被归档，build/release dry-run 不被调度为成功

### Requirement: CI artifacts 可复现并可诊断失败
每个 gate SHALL 保留 result/log；pipeline MUST 归档 JUnit/coverage、trace、eval、smoke logs、wheel/sdist、checksum、license report 和 release preview，并用相对路径与受审 commit/diff identity 绑定。

Evidence producer 与 acceptance matrix validator SHALL 复用同一个输入身份实现；该身份 MUST 覆盖 tracked binary diff 以及所有未忽略的 untracked 文件路径、mode 和 bytes，生成目录不得污染身份。

通用 result MUST 遵循 `ci-result/v1`；release 原生 artifact MUST 遵循 `release-preview/v1`、`release-promotion-plan/v1`、`release-build/v1`、`release-promotion/v1` 与 `registry-publish-plan/v1`，license 原生 artifact MUST 遵循 `license-report/v1`。消费者遇到未知 major、缺必填字段、非法状态枚举、绝对路径或 checksum 不一致时必须失败；registry plan consumer 只接受 `status: planned`，promotion plan consumer 只接受完整 `planned` 或零授权 `no-release`，正式 build consumer 只接受 `status: built`。

eval 与 smoke gate MUST 生成稳定原生产物：`.artifacts/eval/scores.jsonl`、`.artifacts/eval/traces.jsonl`、`.artifacts/smoke/local/trace.jsonl`、`.artifacts/smoke/service/trace.jsonl`。job contract 不得接受 `native_artifacts_pending` 或其他占位字段；每个原生产物 MUST 同时存在于 job manifest、对应 `ci-result/v1.artifacts[]` 的 SHA-256 清单、GitHub upload-artifact 路径和 GitLab artifacts 路径中。GitHub 固定的 artifact action 若默认排除点目录，所有上传 `.artifacts/**` 的 step MUST 显式启用 hidden-file inclusion；release handoff 同时上传多个 `.artifacts/**` 路径时，其 archive root 为共同祖先 `.artifacts`，consumer MUST 下载到 `.artifacts` 才能恢复 Make target 读取的仓库相对路径。合同 validator 必须拒绝 hidden-file input 缺失、关闭或 release download root/name 漂移，不能把 YAML 中存在 path 冒充 hosted 可消费证据。

#### Scenario: 成功 pipeline 产出完整 artifact 集
- **WHEN** required jobs 全部成功
- **THEN** artifact contract 能找到每类要求的文件、producer job、checksum 和输入 identity
- **AND** 未跟踪源码存在时 producer 与 validator 仍计算出相同 identity

#### Scenario: 失败 job 仍保留诊断但不变绿
- **WHEN** gate 命令失败
- **THEN** CI 保留 result/log artifact，job 与依赖它的下游仍保持失败或未运行

#### Scenario: 同名 gate 重跑不暴露陈旧终态
- **WHEN** 已有 result 的 gate 开始重跑或在重跑中被中断
- **THEN** 旧 `result.json` 从启动到新终态写入之间保持不可见，消费者不能把上一轮 identity 或状态当作当前结论

#### Scenario: 原生产物占位或归档集合漂移时失败
- **WHEN** job contract 出现 `native_artifacts_pending`、稳定原生产物缺失，或 manifest、`ci-result/v1`、GitHub 与 GitLab 任一归档集合不一致
- **THEN** CI contract validator 非零失败，acceptance matrix 不得把该 gate 标为已有可归档证据

### Requirement: pipeline 语义由合同测试和实际 runner 共同证明
系统 MUST 同时验证 YAML 触发/依赖/artifact/权限合同与 `act`、`gitlab-ci-local` 中仓库 Make gate 的真实执行；任一层未运行不得表述为 hosted CI PASS。artifact 上传路径、权限和失败保留语义由静态 pipeline 合同继续 fail closed；本地 artifact service 不属于仓库 gate 的验收依赖，也不能用于证明 hosted artifact 可下载。

#### Scenario: 本地 runner 执行成功路径
- **WHEN** 维护者用固定版本 local runner 执行选定 pipeline jobs
- **THEN** runner 实际启动 job、执行仓库 Make target 并返回该 gate 的真实退出状态；本地 backend 支持时同时验证依赖 artifact 传递

#### Scenario: 本地 artifact backend 不兼容不否定仓库 gate
- **WHEN** 仓库 Make gate 已在 local runner 容器中退出 0，但 `act` 等本地 artifact server 因协议或 action 版本不兼容使后续上传失败
- **THEN** 该限制记录为 `artifact server 能力受限`，仓库 gate 证据可用于本地收口，且不得把整个 job 记为 PASS、不得把 artifact service 记为已验证

#### Scenario: 本地 runner 证明失败阻断
- **WHEN** 注入受控失败使 required gate 非零
- **THEN** release dry-run 不执行，失败 result/log 保留，且测试未修改远端 CI 状态

#### Scenario: hosted 未执行时保留边界
- **WHEN** 本轮未 push 因而没有 GitHub/GitLab hosted run
- **THEN** 文档和验收矩阵明确标记 hosted 权限、runner 和 environment protection 为未验证，不引用本地结果冒充通过

### Requirement: release jobs 使用完整 Git history 与 tags
GitHub release 相关 checkout MUST 使用 `fetch-depth: 0`，GitLab 对应 jobs/pipeline MUST 使用 `GIT_DEPTH: "0"`；release wrapper 前 MUST 证明 checkout 非 shallow 且历史 release tags 可见，不能让平台默认浅克隆改变 first-release、SemVer 或 `no-release` 判断。

#### Scenario: 两套 CI 配置禁用浅克隆
- **WHEN** pipeline contract 解析 GitHub 与 GitLab release 路径
- **THEN** GitHub checkout 明确 `fetch-depth: 0`，GitLab 明确 `GIT_DEPTH: "0"`，两者在调用 release dry-run 前运行非 shallow/tag 可见性门禁

#### Scenario: 真实 depth-1 clone 不能直接预演
- **WHEN** 从含历史 releasable commits 和 release tag 的本地 bare remote 创建 depth-1 clone
- **THEN** 未补全 history/tags 时 wrapper 非零失败；按两套 CI 合同补全后 `git rev-parse --is-shallow-repository` 为 `false`、历史 tag 可见并使用正确版本基线

### Requirement: 本地 ready-to-archive 不冒充 hosted PASS
仓库实现、合同测试、local runner 中的仓库 Make gates 与本地验证证据全部完成时，change 默认还需 frozen review 才可标记为本地授权范围内 `ready-to-archive`。若仓库 owner 对本次变更明确作出一次性最终审查豁免，change MAY 记录 `owner-waived` 后进入本地 `ready-to-archive`，但 MUST NOT 把豁免写成 reviewer PASS，也不得把它扩展为后续变更的默认规则。本地 artifact service 不属于该仓库 gate 的验收依赖。若未 push 触发 hosted runner，AC-053/054 的 hosted execution、artifact service 与远端 environment protection MUST 保持未验证，Product-Spec 不得勾选为通过。

#### Scenario: 本地证据完成但 hosted 未运行
- **WHEN** 两套 pipeline 合同和 local runner 仓库 gates 均通过，frozen review 已完成或 owner 已明确记录本次变更的一次性审查豁免，但本轮按约束没有 push 或远端配置变更
- **THEN** change 可记录本地 ready-to-archive；若采用豁免则同时记录 `owner-waived` 而非 reviewer PASS。acceptance matrix 将 hosted execution、artifact service、远端 reviewer/protected ref/secret 标为 `hosted-unverified`，且不声明已发布或已归档

### Requirement: 发布权限遵循最小权限和人工门禁
普通 CI MUST 只有只读仓库权限且不能访问 promotion/publish credential。可发布输入的真实发布 DAG MUST 固定为 `promote-plan -> promote-execute -> publish-plan -> publish-execute`；无版本变化输入 MUST 从同一 promotion plan 分流到无 environment、无 credential 的 no-release 终止节点，且不得调度任何 registry job。plan job MUST 只读、不得读取任何 push/provider/registry credential；registry plan 原子生成状态为 `planned` 的 `registry-publish-plan/v1`，promotion plan 对可发布输入生成 `planned`，对无版本变化输入生成零授权 `no-release`。planned execute job MUST 显式手动触发、依赖全部质量门禁、绑定受保护 environment/ref 并下载同一 plan artifact，在任何副作用前重新计算动态 `approval_sha256`；no-release 节点只消费同一 plan 并生成零副作用回执。`promote-execute` MUST 在 release commit/tag/release notes 后从 tag target 生成状态为 `built` 的 `release-build/v1`，`publish-plan` 与 `publish-execute` 不得使用 preview wheel/sdist。GitHub MUST 通过 plan output 条件化 planned/no-release jobs；GitLab MUST 由无凭据 plan 生成只实例化对应节点的动态 child pipeline。GitHub `promote-execute` MAY 单独使用 `contents: write`，但在 checkout `persist-credentials: false` 时 MUST 由 protected environment 注入与冻结 HTTPS host 绑定的短期 push credential，且不得读取 registry credential；`publish-execute` 恢复 `contents: read`。GitLab planned child 的 execute jobs MUST 使用受保护、分权、短期或最小 scoped credential，不得假定普通 `CI_JOB_TOKEN` 默认拥有 Git push 权限。

#### Scenario: 普通 CI 不具备发布权限
- **WHEN** push、pull request 或 merge request 运行普通 pipeline
- **THEN** job 不读取 promotion/registry credential，不具有写 contents/tag/release 权限，也不执行 promotion 或 publish wrapper 的 execute 模式

#### Scenario: plan artifact 动态交接审批身份
- **WHEN** 两套 CI 进入 promotion 或 publish 的人工门禁
- **THEN** plan job 先在无 credential 的只读环境原子写出状态为 `planned` 的版本化 plan artifact，execute job 只从该 artifact 读取本轮 `approval_sha256`，并对 preview/build/receipt、source、endpoint、protected ref 与 credential 前置条件重新计算一致性

#### Scenario: no-release 在无凭据路径成功终止
- **WHEN** promotion plan 状态为零授权 `no-release`
- **THEN** GitHub 只调度无 environment/secret 的 no-release job，GitLab 生成只含无凭据回执 job 的 child pipeline；两者成功写出 `status: no-release` 回执，不调度 planned execute、registry plan 或 registry execute

#### Scenario: execute 缺少受限 credential 时零副作用失败
- **WHEN** GitHub 缺少短期 push credential、GitLab 缺少受保护 scoped push credential，或任一 publish execute 缺少 registry credential
- **THEN** execute 在创建 commit/tag/release、push 或上传前非零失败，诊断只列缺失的配置名称与恢复边界，不回显 credential

#### Scenario: GitHub promotion 与 registry publish 分权
- **WHEN** 维护者在受保护 environment 中显式启动真实 release 流程
- **THEN** `promote-execute` 只获得创建 release commit/tag/release 所需的 `contents: write`、短期 push credential 与 provider credential，且看不到 registry secret，并按 tag 后构建顺序产出 `release-build/v1` 与 `release-promotion/v1`；`publish-execute` 只读仓库并用 registry credential 消费状态为 `promoted` 且与 preview/build/artifacts 完整匹配的回执

#### Scenario: promotion 回执不闭合时两套 CI 都阻断 publish
- **WHEN** GitHub 或 GitLab publish job 收到 `no-release`、`failed`、陈旧回执，或 preview/build manifest、source、release commit/tag target、provider release、正式 artifact checksum 任一不一致
- **THEN** 正常 DAG 不调度 registry job；若防御性 consumer 被异常调用，则在发起上传前失败，诊断去敏并保留回执 identity

#### Scenario: publish 配置缺远端保护时不得宣称 hosted ready
- **WHEN** YAML 引用了 environment/manual gate 但远端 reviewer、protected ref 或 secret 未经 hosted 验证
- **THEN** acceptance evidence 只记录配置合同与本地执行通过，并列出平台管理员必须完成的前置条件，不宣称 hosted publish ready

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
