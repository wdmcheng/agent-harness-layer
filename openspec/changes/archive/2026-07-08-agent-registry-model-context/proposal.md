## Source Links

- Product-Spec.md：`FLOW-001` 本地 fake/mock provider；`FLOW-002` 新增 agent；`REQ-007` 多 agent registry 与受控 delegation；`REQ-008` API / CLI 管理面；`REQ-012` 模型、预算、上下文组装与 embedding；`REQ-013` Retrieval 与 RAG 的 Phase 6 边界。
- DEV-PLAN.md：`Phase 6: Agent Registry、模型路由与 Embedding`；Spec 覆盖矩阵中 `REQ-002`、`REQ-007`、`REQ-012`、`REQ-013`。
- Design-Brief.md or design artifact：无产品化前端 UI；仅沿用架构图中的 Agent Loop、Model Provider、Context Assembly、Embedding / Retrieval 边界。
- CONTEXT.md / ADR：当前无相关领域上下文或 ADR。

## Why

Phase 5 已打通 run lifecycle，但 runtime 仍缺少可枚举、可校验的 agent 描述和 provider-neutral 模型/embedding 接缝。Phase 6 需要把多 agent、模型路由、上下文组装和 embedding cache 建成公共边界，否则后续 tool、retrieval、policy 和 eval 会继续绕过核心包。

## What Changes

- 新增 agent registry 能力：`AgentDescriptor`、多个 agent config 加载、重复 `agent_id` 拒绝、descriptor 可见字段、delegation edge 查询。
- 固定 `config.yaml` 必填字段：`agent_id`、`version`、`name`、`description`、`input_schema`、`output_schema`、`model` 策略、`budget`、`tool_allowlist`、`eval_dataset`、`delegation_edges`；API 只暴露 public descriptor 和相对 `config_ref`，不暴露本地绝对路径、secret、callable 或 provider client。
- 新增 delegation 归并记录 seam：声明过的 delegation edge 允许创建 delegated run 归属摘要，parent run 可追踪 delegated usage、budget 和 trace refs；未声明 edge 默认拒绝。
- 扩展 CLI 和 service API：新增 `agent-harness agents list` 和 `GET /api/v1/agents`，并先补齐 `API-Contract.md` 的 `AGT-001` 完整 endpoint 契约与局部 OpenAPI drift tests。
- 新增 provider-neutral 模型接缝：`ModelProvider`、`FakeModelProvider`、Pydantic AI adapter 边界、`ModelRouter`、timeout、fallback、预算估算和显式 reload/restart seam。
- 新增 `ContextAssembler` 和 token budget 能力，统一记录 source、trust_level、token budget、truncation 与 fallback decision，并把 assembly metadata 写入 `context_assemblies` 记录。
- 新增 embedding 接缝：`EmbeddingProvider`、mock/local embedding、OpenAI-compatible adapter 和 `embedding_cache` 持久化记录。
- 扩展 import boundary，证明业务 agent 和 runtime core 不直接 import `pydantic_ai`，只允许 adapter 边界接触 provider SDK。

## Non-Goals

- 不实现 Phase 7 的认证、PolicyEngine、HITL approval 或 approval resume。
- 不实现 Phase 8 的 ToolRegistry、FileTool、ShellTool 或 MCP client。
- 不实现 Phase 9 的 RetrievalProvider、RAG 索引、PGroonga 或 pgvector adapter；Phase 6 只接收 retrieval chunk 形状并记录 context assembly trace。
- 不实现复杂 multi-agent graph workflow；本 change 只提供 registry 和受控 delegation 配置读取/校验 seam。
- 不做产品化前端 UI，不新增真实 SaaS provider 依赖为本地 smoke 的必要条件。

## Capabilities

### New Capabilities

- `agent-registry-model-context`：多 agent registry、agent list API/CLI、模型路由、上下文组装、embedding provider/cache 和 provider import 边界。

### Modified Capabilities

- 无。本 change 新增 Phase 6 能力，并通过兼容方式消费 Phase 5 runtime seam。

## Impact

- 受影响代码：`packages/agent-harness/src/agent_harness/registry/**`、`models/**`、`context/**`、`embeddings/**`、`adapters/models/**`、`agent_harness/cli.py`、`templates/service-app/app/api/routes/agents.py`、`templates/service-app/app/runtime.py`、模板 agent config。
- 受影响契约：`API-Contract.md` 的 `AGT-001`，OpenAPI drift contract tests。
- 受影响测试：registry/model/context/embedding contract tests、CLI tests、service-app OpenAPI tests、import boundary tests、smoke-local / smoke-service。
- 受影响数据：新增 `context_assemblies` 与 `embedding_cache` 存储表 / repository seam；service smoke 要区分 SQLite local 与 PostgreSQL service profile 证据。
- 受影响配置：`templates/service-app/agents/**/config.yaml` 和 profile/model/embedding 相关配置字段。
