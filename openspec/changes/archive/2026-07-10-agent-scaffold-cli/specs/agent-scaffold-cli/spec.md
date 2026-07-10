## ADDED Requirements

### Requirement: Scaffold CLI 生成可加载的 Agent 目录
`agent-harness scaffold agent <agent_id>` SHALL 在解析出的 `agents_dir` 下生成一个符合 registry、runtime 和 eval 目录契约的 agent package。`agent_id` MUST 使用以小写字母开头的点分 Python identifier，每段只含小写字母、数字和下划线；点分段映射为目录层级。显式 `--agents-dir` 优先；省略时 CLI MUST 从当前目录向上识别 service-app root（`pyproject.toml` 的 project name 为 `agent-harness-service-app` 且存在 `agents/`），并使用 `<service-app-root>/agents`。仅在源仓库根目录识别到受控 workspace markers 时，才可 fallback 到 `<repo-root>/templates/service-app/agents`；无法唯一识别时 MUST 失败并提示 `--agents-dir`，不得基于硬编码相对路径创建嵌套目录。

#### Scenario: 成功生成完整 agent package
- **WHEN** 开发者执行 `agent-harness scaffold agent support.triage --agents-dir <root>`
- **THEN** 命令创建 `<root>/support/triage/`，其中包含 `__init__.py`、`agent.py`、`tools.py`、`schemas.py`、`config.yaml`、`evals/drafts/example.yaml` 和空 `evals/approved/`，并输出相对创建路径

#### Scenario: 复制模板中省略 agents-dir 使用项目根
- **WHEN** 开发者在 workspace 外复制的 service-app root 执行 `agent-harness scaffold agent support.triage` 且不传 `--agents-dir`
- **THEN** 命令只创建 `<copied-root>/agents/support/triage/`，不得创建 `<copied-root>/templates/service-app/agents`、仓库外 sibling 或其他猜测路径

#### Scenario: 无法发现唯一项目根时明确失败
- **WHEN** 当前目录既不是 service-app root/子目录，也不是受控源仓库，且调用方未传 `--agents-dir`
- **THEN** CLI 非零退出并提示传入 `--agents-dir`，filesystem 保持不变

#### Scenario: 生成结果可被 registry 加载
- **WHEN** `AgentRegistry` 从 `agents_dir` 加载刚生成的 config
- **THEN** public descriptor 的 `agent_id`、schema refs、fake model、预算、空工具白名单、eval dataset 和空 delegation edge 均通过校验，且 agent executor reference 指向生成 package 内受控入口

#### Scenario: 生成 executor 可通过真实 runtime 完成 run
- **WHEN** contract test 在 local/fake composition 中解析刚生成 agent 的 executor，并通过 `RunOrchestrator` 执行示例 input
- **THEN** 同一 run 产生真实 typed output、completed status 和唯一 terminal event，不得依赖固定 `fake-ok` fallback 或模板源码路径

### Requirement: Scaffold 默认内容保持离线与安全边界
生成 agent SHALL 只依赖 `agent_harness` 公共接口，默认使用无需 API key 的 fake model、空工具 allowlist、空 delegation edges 和可审阅 draft eval。命令 MUST NOT 生成或写入 provider secret，MUST NOT 把 draft case 自动写入 approved dataset。

#### Scenario: 静态边界扫描通过
- **WHEN** boundary test 扫描所有生成 Python/YAML 文件
- **THEN** 不存在 vendor SDK、ORM session、secret、绝对路径或危险工具默认权限，config 可在 local profile 下解析

#### Scenario: Draft 与 approved 边界保持
- **WHEN** scaffold 生成 eval 目录
- **THEN** 示例 case 只位于 drafts，approved 目录为空，后续必须通过人工 review/approve 流程写入 approved dataset

#### Scenario: 人工 approve 后由现有 EvalRunner 执行
- **WHEN** contract test 通过现有人工 review/approve seam 把生成 draft 写入 approved dataset，再调用 `EvalRunner` 的 approved case executor
- **THEN** 只执行 approved case，生成 agent 的真实 executor output 与 expected 比较通过并留下 local score/trace evidence；scaffold 命令本身仍不得自动 approve 或运行 eval

### Requirement: 已生成 Agent 在 runtime 回滚时保持 fail-closed
scaffold 生成的 agent package SHALL 显式依赖受控 `AgentExecutor` reference。应用回滚不得在仍有受管 agent 依赖该 contract 时移除 executor resolver/runtime seam；compatibility preflight MUST 扫描所有受管 `agents_dir`、列出不兼容 `agent_id` 并阻止回滚。系统 MUST NOT 自动删除、改写或降级已生成 agent，也 MUST NOT 恢复无 executor 时的固定 fake output；操作者只能保留 compatibility seam，或显式迁移/带审计地隔离 agent。

#### Scenario: 未迁移的生成 Agent 阻止 runtime 回滚
- **WHEN** scaffold 已生成且成功运行一个 agent，回滚流程准备移除该 agent config 依赖的 executor resolver/runtime seam
- **THEN** compatibility preflight 非零失败并列出对应 `agent_id`，agent 文件保持不变且在当前 runtime 仍可加载/运行，不产生固定 fake output

#### Scenario: 显式迁移或隔离后才允许继续
- **WHEN** 操作者把所有受影响 agent 迁移到目标 runtime 支持的显式 executor contract，或带审计地隔离到 registry 扫描根外并保留原文件
- **THEN** compatibility inventory 不再报告活动的不兼容 agent，回滚流程才可继续；系统不替操作者删除或改写 package

### Requirement: Scaffold 拒绝路径穿越与覆盖
scaffold SHALL 在写文件前校验 agent id、目标相对路径和目标不存在；绝对路径、空段、`.`/`..`、路径分隔符、符号链接逃逸、无效 Python identifier 或已存在目标 MUST 被拒绝，且不得改变目标外文件。

#### Scenario: 非法 agent id 被拒绝
- **WHEN** 调用方传入包含绝对路径、`..`、斜杠、空段、大写开头、连字符或其他非法字符的 agent id
- **THEN** 命令返回非零退出码和稳定错误摘要，`agents_dir` 内容不变

#### Scenario: 已存在目录不被覆盖
- **WHEN** 目标 agent 目录已经存在
- **THEN** 命令返回非零退出码，不修改、删除或合并任何现有文件

#### Scenario: 父路径符号链接逃逸被拒绝
- **WHEN** 点分 agent id 映射的任一父路径是指向 `agents_dir` 外部的符号链接
- **THEN** 命令拒绝创建，外部目标目录不产生文件

### Requirement: Scaffold 写入具有原子失败语义
scaffold SHALL 先在 `agents_dir` 的同文件系统 sibling staging namespace 中渲染和验证完整 package；该 namespace MUST 位于正常 `AgentRegistry.load_from_directory(agents_dir)` 的递归扫描根之外。验证完成后再原子 rename 发布目标；任何渲染、schema、registry 或文件系统错误 MUST 清理 staging，且不得留下半成品目标。

#### Scenario: 发布前正常 registry 看不到 staging
- **WHEN** scaffold 在测试暂停点已把 `config.yaml` 写入 sibling staging、但尚未 rename 到目标，同时另一个调用方正常扫描 `agents_dir`
- **THEN** registry 不返回 staging descriptor、不因 staging 内容报 validation error，目标 agent 仍不可见

#### Scenario: 验证失败不留下半成品
- **WHEN** 生成内容在发布前 registry/schema 验证失败
- **THEN** 命令返回非零退出码，目标目录不存在，临时 staging 内容被清理

#### Scenario: 发布成功后才可见目标
- **WHEN** 所有文件渲染和验证通过
- **THEN** 目标目录通过一次 rename 出现，随后 registry 可立即加载完整 package

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
