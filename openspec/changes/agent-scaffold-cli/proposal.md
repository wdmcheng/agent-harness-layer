## Source Links

- Product-Spec.md: `FLOW-002` 新增一个 agent、`SCOPE-027` CLI、`REQ-002` 核心包与上游隔离、`REQ-008` P0 CLI 命令集。
- DEV-PLAN.md: 技术栈 Typer CLI 决策，以及 Phase 12 的 CLI 命令集收口边界。
- Design-Brief.md or design artifact: 不适用；本 change 仅新增 CLI 文件脚手架，不涉及 UI。
- CONTEXT.md / ADR: 当前仓库无相关文件。

## Why

Phase 12 盘点发现 doctor、agents、run、approvals、eval 和 policy CLI 已存在，但 Product Spec 明列的 `agent-harness scaffold agent <agent_id>` 缺失。没有受控 scaffold，开发者只能手工拼装 config/schema/eval 目录，容易制造 registry 无法加载或越过 vendor 边界的 agent。

## What Changes

- 新增 Typer `scaffold agent` 命令，在受控 `agents_dir` 下创建一个可被 `AgentRegistry` 加载的 agent 目录。
- 生成 `agent.py`、`tools.py`、`schemas.py`、`config.yaml`、`evals/drafts/example.yaml`、空的 `evals/approved/` 和包初始化文件，默认使用 fake model、空工具白名单和安全预算；命令不得把未审核 case 写入 approved dataset。
- 校验 `agent_id` 和目标路径，拒绝路径穿越、绝对路径、无效标识以及覆盖现有目录。
- 生成后立即用 registry/config/schema contract 验证；失败时不留下半成品目录。
- 增加 CLI help、成功创建、重复创建、非法 id、原子失败和 vendor boundary tests，并在模板 README 记录入口。

## Non-Goals

- 不实现交互式问答、远程模板下载、代码生成模型调用或自动注册 provider secret。
- scaffold 命令本身不自动运行 agent、不自动 approve/write eval 结果、不提交 git、不 push、不部署；contract/Phase 验收仍必须通过公开 runtime 与人工 approve/EvalRunner 证明生成物可用。
- 不修改四个 P0 示例 agent 逻辑，不实现通用 plugin ABI 或复杂 multi-agent workflow。

## Capabilities

### New Capabilities

- `agent-scaffold-cli`: 定义安全、原子、可验证的 agent 目录生成命令和默认边界。

### Modified Capabilities

- 无。

## Impact

- 影响 `packages/agent-harness/src/agent_harness/cli.py`、新增 scaffold helper/module、CLI contract tests 和模板 README。
- 只使用标准库、Typer、Pydantic/YAML 与现有 registry 公共 seam，不新增外部依赖或数据库 migration。
- 生成内容优先位于调用方显式选择的 `agents_dir`；省略时从当前目录发现 service-app root并使用其 `agents/`，只在受控源仓库根才 fallback 到 `templates/service-app/agents`，无法唯一发现时失败。
