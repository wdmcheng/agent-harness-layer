# 框架定位与能力对照说明

[English](framework-positioning.md) | [简体中文](framework-positioning.zh-CN.md)

导航：[根 README](../README.zh-CN.md) · [开发 Agent](building-an-agent.zh-CN.md) · [架构图](architecture/README.zh-CN.md) · [扩展指南](extension-guide.zh-CN.md)

本文补充一个“五层两翼”指南没有展开的问题：Agent Harness Layer 与 Pydantic AI、`pydantic-ai-harness`、Agently 分别是什么关系，以及其他框架的能力设计哪些值得借鉴、哪些不能直接搬进 service-app。

先给结论：本仓库是企业级控制面，不是任一参考框架的薄封装。Pydantic AI 是面向模型的基础；`pydantic-ai-harness` 是围绕 Pydantic AI 组合能力的库；Agently 是拥有自己 request、Action、Skill、task 和 workflow 所有权的完整 AI 应用运行时。本项目负责 identity、policy、HITL、durable run、checkpoint、budget、tenant boundary、本地优先 evidence、eval acceptance 和 release gate。

## 先看能力对照的阅读边界

本文用于解释能力边界和设计差异，不是其他 runtime 的复制粘贴教程。对本项目来说：

- 业务 Agent 只能使用 `agent_harness` 的公开 DTO、protocol、registry、facade、repository 和 UoW；
- vendor SDK 与其他框架 runtime 对象只能停留在 adapter 或明确批准的 integration boundary；
- tool 必须经过 `ToolRegistry` 和 `PolicyEngine`，危险动作可能进入 HITL waiting；
- sub-agent 必须经过 `AgentRegistry`、delegation edge 和 shared-parent budget；
- 本地 `CanonicalEvent`、usage、audit、checkpoint 和 eval evidence 仍是事实源；
- 其他框架“有这个能力”不等于本项目已经启用、支持或验证了它。

## 本仓库的 Quick Start

首次创建 Agent 使用 service-app 模板，这是当前支持的路径：

```bash
cd templates/service-app
make bootstrap

export AGENT_HARNESS_BUDGET__FINGERPRINT_KEY="$([[ -n \"$AGENT_HARNESS_BUDGET__FINGERPRINT_KEY\" ]] && printf %s \"$AGENT_HARNESS_BUDGET__FINGERPRINT_KEY\" || uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export STATE_DIR="$PWD/.agent-harness/local"
export STORAGE_DSN="sqlite+aiosqlite:///$STATE_DIR/agent_harness.db"
mkdir -p "$STATE_DIR"

uv run python app/migrate.py \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness scaffold agent support.triage
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"
uv run agent-harness run support.triage \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --prompt 'login stopped working'
```

初始化、API、eval、observability 和交接细节仍以 [service-app README](../templates/service-app/README.zh-CN.md)、[示例 Agent](../templates/service-app/docs/examples.zh-CN.md) 和 [AI / Agent 项目操作指南](../templates/service-app/docs/ai-agent-guide.zh-CN.md) 为准。不要用其他框架的全局 settings 或执行循环替换这条路径。

## 能力矩阵：当前、参考与未采用

| 能力 | Agent Harness Layer 当前状态 | Pydantic AI Harness 参考 | Agently 参考 | 本项目决策 |
|---|---|---|---|---|
| Model request 与 structured output | provider adapter 较薄，公开 response 不是其他框架的完整 runtime object | Pydantic AI capability/hook 在 `Agent` 层组合 | `.output(...)`、解析、校验、重试和 result view | 业务代码保持 vendor-neutral；语法糖必须接现有 model seam |
| Tool 执行 | `ToolRegistry` → policy/workspace → 可选 HITL → audit/artifact | `CodeMode`、MCP、search、filesystem、shell capability | Action Runtime 与 ExecutionResource | 不绕过 registry 或 approval |
| Sub-agent | 声明式 delegation edge、checkpoint、shared-parent budget | `DynamicWorkflow` 与 sub-agent pattern | AgentTask、TaskDAG、TriggerFlow、team pattern | delegation 保持显式且可恢复 |
| Workflow | `RunOrchestrator`、queue、checkpoint、resume、service worker | DynamicWorkflow 是参考 runtime 负责的模型生成 workflow | TriggerFlow 与 Dynamic Task | 不隐式增加第二套 scheduler |
| Skill | `.agents/skills` 是开发期说明；runtime Skill catalog 不是当前公开 seam | Skills 是参考 capability area | SkillLibrary、精确 revision、TaskContext disclosure | 若引入，必须使用可信 revision、现有 policy/audit |
| Memory/session | context assembly、repository/UoW、checkpoint、tenant/run correlation | Memory、sliding window、compaction、persistence | Session、TaskContext、records、snapshot | 不允许隐藏模型调用或第二套持久化真相 |
| Eval | approved-only case、experiment、人工 acceptance、`needs_review` | capability matrix 包含 verification/eval building block | evaluator/reviser 与 task evidence pattern | 保持人工批准为 gate |
| Observability | local `CanonicalEvent`、usage、audit 优先，可选 provider fan-out | Logfire 与 capability trace | observation event、DevTools、execution record | provider degraded 不能抹掉本地 evidence |
| Release/governance | license、artifact、CI evidence、local/service/hosted 边界 | 参考 package release policy | runtime framework version/release policy | release 真相留在本仓库 |

这张表是边界图，不是“安装清单”。它防止最常见的错误：看到其他框架的 capability matrix，就悄悄引入第二套 runtime。

## 为什么用本项目，而不是直接用其他框架 runtime？

当应用需要 identity、tenant isolation、permission、approval、durable recovery、budget、audit、本地优先 evidence、eval acceptance 或 service deployment 的稳定所有权时，使用本项目。只有一两个 prompt、产品边界就是 provider-native 能力时，直接用 Pydantic AI 更合适。需要某个窄 capability 时，研究 `pydantic-ai-harness` 的语义并在 adapter 中选择性接入。需要 structured output、Action/Skill 生命周期、task evidence 或 signal-driven workflow 的参考实现时，研究 Agently。

它们的差异是重心不同：

| 重心 | 强项 | 为什么不是本项目默认方案 |
|---|---|---|
| Direct SDK/Pydantic AI | 表面积小、provider-native 能力直接 | 企业 runtime、治理和 evidence ownership 留给应用自行解决 |
| Pydantic AI Harness | 围绕 Pydantic AI 组合 capability bundle | 本仓库 pin 的 Pydantic AI 基线不同，policy/tool/runtime 所有权也不同 |
| Agently | request、Action、Skill、task、workflow 一体化 runtime | 会引入重叠的 execution、persistence 和 lifecycle contract |
| Agent Harness Layer | 围绕业务 Agent 的 governed service boundary | 有意更窄，其他框架能力必须经过明确 adapter |

## 阅读与采用顺序

先读参考语义，再回到本仓库对应合同：

1. 先读本仓库的[五层两翼指南](building-an-agent.zh-CN.md)和模板 Quick Start。
2. 再读 Pydantic AI Harness 的 [README](https://github.com/pydantic/pydantic-ai-harness/blob/main/README.md)，重点看 Quick start、DynamicWorkflow 和 Capability matrix。
3. 再读 Agently 的 [Why Agently](https://github.com/AgentEra/Agently#why-agently)、[Framework Positioning](https://github.com/AgentEra/Agently#framework-positioning) 和 [Quickstart](https://github.com/AgentEra/Agently#quickstart)。
4. 把目标行为映射到[扩展指南](extension-guide.zh-CN.md)、[Adapter 合同](adapter-contracts.zh-CN.md)、[Context 与信任边界](context-and-trust-boundary.zh-CN.md)和[安全策略](security-policy.zh-CN.md)。
5. 如果能力会改变公开 contract、持久化 owner、budget 含义或安全边界，就不能继续当作文档补充；应先单独设计再实现。

参考页面会独立变化；这里的链接只用于理解方向。本仓库的 pin、公开 export、测试和 release evidence 才决定当前 checkout 能运行什么。
