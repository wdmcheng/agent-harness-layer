## Context

核心 CLI 已具备 doctor、agents、run、approvals、eval 和 policy，缺少 Product Spec `FLOW-002` 指定的 scaffold agent 入口。registry config 字段、fake model、工具 allowlist、eval review gate 已稳定；该命令的关键风险不是模板渲染，而是路径穿越、覆盖用户文件、半成品目录和把 draft 自动写入 approved。

## Goals / Non-Goals

**Goals:**

- 用一个非交互 Typer 命令生成 registry 可加载、local/fake 可运行的 agent package。
- 将路径、覆盖、原子发布和 eval review 边界做成确定性行为与测试。

**Non-Goals:**

- 不调用模型生成代码，不联网下载模板，不自动运行或批准 eval，不修改 git/external state。

## Decisions

1. **agent id 使用点分 Python identifier。** 每段匹配 `[a-z][a-z0-9_]*`，点映射目录层级，既对齐 `examples.rag_assistant` 先例，也能生成合法 import ref。替代方案是允许任意 slug；拒绝，因为连字符和路径字符会产生不可导入或不安全目录。
2. **模板放在核心 scaffold 模块的结构化常量/渲染函数中。** 每个文件由明确 renderer 生成，代码与配置说明使用项目主语言，避免引入模板引擎依赖。替代方案是复制模板目录；拒绝，因为容易携带示例私有状态和 approved 数据。
3. **先在 registry 扫描根之外 staging 验证，再原子发布。** 在 `agents_dir.parent/.agent-harness-scaffold-staging/<uuid>/` 创建 synthetic `agents/` package 树；它与目标同一 filesystem，但不位于正常 `AgentRegistry.load_from_directory(agents_dir)` 的递归根内。写完后把 staging root 临时加入受控 import context，运行 registry/config/schema/executor validation，成功后将具体 agent 目录 rename 到目标。命令启动时校验 staging/target parent 的 device id 一致；任何异常在 finally 清理 staging。替代方案是在 `agents_dir` 内使用点前缀目录；拒绝，因为 registry 的 `rglob("config.yaml")` 不会自动忽略隐藏目录，仍可能观察半成品。
4. **父路径逐段解析并拒绝 symlink escape。** `agents_dir` 和已存在父目录均 resolve 后检查仍在 root 内；不跟随外部 symlink 创建。目标存在一律失败，不提供 force，避免命令成为覆盖工具。
5. **eval 只生成 draft。** `config.eval_dataset` 指向空 approved 目录或 approved dataset ref，示例输入放 `evals/drafts/example.yaml`；命令不伪造 reviewer/audit，也不写 approved case。
6. **生成 executor 与 Phase 12 execution seam 对齐。** `agent.py` 暴露受控 executor callable，config 保存 package 内 module ref；如果 `p0-example-agent-flows` executor contract 尚未实现，apply 顺序先完成该公共 seam再启用 scaffold 生成/验证。
7. **默认 root 通过项目标记发现，不通过相对层级猜测。** 显式 `--agents-dir` 优先；否则向上寻找 `agent-harness-service-app` pyproject + `agents/`，源仓库 fallback 还必须同时匹配 workspace markers。复制模板内从任何子目录调用都落到该项目的 `agents/`；歧义或无标记时失败。替代方案是默认 `templates/service-app/agents`；拒绝，因为复制项目会生成错误嵌套路径。

## Affected Surfaces

- `agent_harness.cli` 新增 `scaffold` Typer group；新模块承载 id 校验、render、staging 与发布。
- CLI unit/contract tests、模板 README 的新 agent 指南。
- 不修改数据库、API、provider adapter 或现有 agent 目录。

## Testing Seams

- Typer `CliRunner`：help、成功、invalid id、existing target、filesystem/validation failure。
- 临时 `agents_dir`：目录结构、内容、无 secret/vendor import、approved 为空、staging 清理。
- `AgentRegistry.load_from_directory`、executor resolver 与 `RunOrchestrator`：生成结果可加载、public descriptor 正确，并由真实 executor 产生 terminal output。
- 人工 review/approve + `EvalRunner`：draft 不自动批准，批准后只执行 approved case并留下 score/trace。
- symlink escape fixture：外部目录保持无改动。

## Risks / Trade-offs

- [风险] staging 自己被 registry 递归扫描。→ staging 固定为 `agents_dir` 的 sibling namespace，不在正常 registry root 下；并发暂停点测试在 staging 已含 config 时扫描正式 root，必须证明无 descriptor/validation error。
- [风险] package 父目录不存在导致非原子多级创建。→ 只创建必要父目录且不写业务文件；目标 agent 目录本身一次 rename 发布，失败时清理本轮新建空父目录。
- [风险] 生成内容随 registry schema 漂移。→ renderer 旁的 contract test 直接调用当前 registry/config/executor resolver，不靠字符串快照自证。
- [风险] 默认 root 在复制项目与源仓库含义不同。→ 用项目标记发现和 copy-out tests固定两种路径；没有唯一 marker 就要求显式参数。

## Migration Plan

该命令是新增 CLI，无数据迁移。实现依赖 `p0-example-agent-flows` 提供 executor contract；若先独立验证，可暂以 registry config 形状为门禁但不得提交不可运行的最终模板。回滚 scaffold command/module/tests 不得删除或改写已生成目录。

整个应用回滚 executor runtime 前必须对所有受管 `agents_dir` 做 compatibility inventory。若任一已生成 config 仍声明新 executor reference，preflight 必须列出受影响 `agent_id` 并阻止移除 resolver/runtime seam；调用方可保留 compatibility seam，或显式迁移/带审计地隔离 agent 到 registry 扫描根外，但系统不得自动迁移、删除或回退到固定 fake output。contract test 必须生成并运行一个 agent，证明未迁移时回滚被拒绝且原 agent 仍可加载/运行，完成显式迁移或隔离后才允许继续回滚。

Phase 收口的 task checkbox 必须在 final reviewer 启动前全部完成并冻结；review PASS/FAIL 属于外部门禁状态，不设计成“review 后再勾”的自引用 task。finding 或任何受审 diff 会使组合验收和 review 失效；PASS 后只能设置 clean-state 与提交，不再编辑受审内容。

## Open Questions

- 无阻塞问题；P0 不提供 `--force`、交互式配置或远程模板源。
