## Source Links

- Product-Spec.md: `FLOW-002` 新增 agent、`FLOW-003` 危险动作审批与恢复，`REQ-002` 扩展与依赖边界、`REQ-004` executor 配置、`REQ-005` storage/migration/UoW、`REQ-006` checkpoint/resume、`REQ-008` API/CLI composition、`REQ-010` Policy/HITL approval、`REQ-011` tool execution、`REQ-013` Retrieval 与 RAG、`REQ-017` 示例 agent，以及 `AC-004`、`AC-008`、`AC-013`、`AC-017`、`AC-033`、`AC-036`、`AC-046`、`AC-047`。
- DEV-PLAN.md: `Phase 12: Service App 模板与四个 P0 示例 Agent` 中四个薄样例、registry、local run、fake model eval 和 vendor boundary 验收。
- API-Contract.md: agent list、run、approval/policy 与 eval 的既有 P0 契约；本 change 不新增 endpoint。
- Design-Brief.md or design artifact: 不适用；示例通过 API/CLI 暴露，不新增视觉 UI。
- CONTEXT.md / ADR: 当前仓库无相关文件；agent 能力与边界以上述产品和计划为准。

## Why

当前模板只有 registry smoke agent 和 RAG config/eval 基础，尚不能用真实薄样例证明 retrieval、结构化输出、workspace file tool、shell/HITL 等扩展点。Phase 12 需要四个彼此不同、可离线确定性验证的示例，为后续 Phase 12.5 提供真实行为分布而不提前实现优化闭环。

## What Changes

- 实现 RAG assistant、ticket triage、repo analyst、dev assistant 四个可运行薄样例 agent。
- 每个示例提供完整 registry config、工具策略、approved fake-model eval cases、公开执行 seam、测试和 trace evidence。
- RAG assistant 证明 local BM25、citation、untrusted context 和无结果降级。
- Ticket triage 证明结构化输出 schema、分类 eval 与 `unknown`/`needs_review` 降级。
- Repo analyst 证明 workspace-safe file read/search、shell 禁用和长输出 `artifact_ref`。
- Dev assistant 证明 shell/file tool 的 policy/HITL、approval/audit 和危险动作拒绝/等待。
- 让 `agent-harness agents list` 和 local `agent-harness run <agent_id>` 能发现并运行四个示例，且无需真实 API key。
- 新增 provider-neutral `AgentExecutor`/execution result seam 和受控 executor resolver：registry config 只保存模块引用，public descriptor 不暴露 callable；CLI/API 仍经 `RunOrchestrator` 创建 run、事件、checkpoint 和终态。
- 把本次触及的 registry、auth/approval、tool execution 主规格中的临时阶段标签改为稳定 capability、endpoint 和行为名称；只做长期契约措辞归一，不改变既有行为语义。

## Non-Goals

- 不把示例扩展成完整产品、通用 workflow engine 或复杂 multi-agent graph。
- 不实现 Phase 12.5 的 behavior tags、optimization/holdout、baseline/candidate compare 或人工 harness acceptance。
- 不新增 provider SDK 直连、远程 tool HTTP route、API/worker 物理拆分、任意第三方 plugin marketplace/发现协议或生产部署。
- 不 archive 本 change 或三个前序 complete changes。

## Capabilities

### New Capabilities

- `p0-example-agents`: 定义四个示例 agent 的可发现、可运行、能力差异、安全降级、approved eval 和 trace evidence 契约。

### Modified Capabilities

- `agent-registry-model-context`: 为 agent config 增加受控、非公开的 executor module reference，并保证 public descriptor 仍不泄漏 callable/module object。
- `runtime-checkpoint-runs`: 让 `RunOrchestrator` 调用注入的 provider-neutral executor，以真实 output、waiting checkpoint/approval 或 failure 完成 run lifecycle。
- `tool-execution-boundaries`: 为 approval resume 增加与 tenant/agent/run/action/resource/args hash 绑定的 `ApprovalGrant` 和持久化单次执行 claim，防止重复 resolve 重放工具副作用。
- `auth-policy-hitl-approvals`: 保持 public approval status 为 waiting/approved/denied/cancelled，同时新增不出现在 API DTO 的私有 resolution lease，协调 approve continuation 与崩溃恢复。

## Impact

- 主要影响 core registry/runtime/approval/tool/eval seam与 migration、`API-Contract.md` 的 APR-002、CLI/API composition，以及 `templates/service-app/agents/examples`、approved eval 数据、示例测试和文档追加项；同时对本次触及的归档主规格做稳定命名清理，不改变其已有 requirement 行为。
- 复用并扩展 `retrieval-rag-foundation`、`eval-gate-trace-loop`、`observability-provider-adapters` 的 active artifacts与公共 seam；RAG 强制通过 `ContextAssembler`，危险动作强制通过 checkpoint + `ApprovalService`，eval 强制通过 `EvalRunner`/`ScoreSink`/`TelemetryFacade`。本 change 不独占既有 RAG config/eval，不自动 archive依赖 change，不得从业务 agent直接 import vendor SDK或 ORM session。
- 新增窄 migration，为 approvals 保存私有 resolution lease/state，为 `tool_invocations` 保存 nullable unique `approval_id` 和执行状态/结果引用；不新增或改变 public approval status/API endpoint，不改变主包对模板的依赖方向。
