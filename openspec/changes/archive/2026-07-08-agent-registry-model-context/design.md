## 背景

当前仓库已完成 profile/config、identity、storage、CanonicalEvent、runtime checkpoint/run API。`agent_harness.config.AgentConfig` 只有单 agent 占位字段，CLI 只有 `doctor` 和 `run`，service app 已有 `/api/v1/agents/{agent_id}/runs` 但没有 `/api/v1/agents` list route。Phase 6 要在不引入真实模型 key 依赖的前提下，把 agent descriptor、模型路由、上下文组装和 embedding cache 建成后续 tools/retrieval/eval 可复用的公共 seam。

## 目标 / 非目标

**目标：**
- 用 Pydantic DTO 固定 `AgentDescriptor`、agent list API/CLI、delegation check、model/context/embedding trace 的边界。
- 保持 local profile 离线可跑，fake model 和 mock/local embedding 是测试与 smoke 的默认路径。
- 把 Pydantic AI / OpenAI-compatible 等 provider 细节隔离到 adapter 层。
- 在 `API-Contract.md` 中先补齐 `AGT-001`，再实现 route 和 OpenAPI drift tests。
- 按 DEV-PLAN 的 Phase 6 数据矩阵新增 `context_assemblies` 与 `embedding_cache` 记录边界。

**非目标：**
- 不做 Phase 7 policy/HITL 的完整审批和权限系统；Phase 6 只提供 delegation 配置读取和拒绝/允许 seam。
- 不做 Phase 8 tools/MCP 或 Phase 9 retrieval index；ContextAssembler 只接受结构化输入片段。
- 不要求 worker 运行中自动热重载；ModelRouter/registry/embedding 配置只需显式 reload 或 restart seam。

## 决策

1. **registry 与 config loader 分层。** `AgentRegistry` 直接读取 `agents/**/config.yaml` 并用 `yaml.safe_load` + Pydantic DTO 校验，复用现有配置安全边界；`load_settings(agent_config_path=...)` 仍保留单 agent 合并能力，不承担多 agent registry。
   - 替代方案：把所有 agent 配置塞进 profile。拒绝，因为会让 profile 承担业务 agent 生命周期，且不利于模板手动新增 agent。

2. **agent list route 只返回 public descriptor。** API DTO 不返回本地文件路径、callable、provider client 或 secret；debug 信息留给 CLI 错误。
   - 替代方案：直接返回完整 YAML。拒绝，因为会把本地目录结构和 future secret 字段暴露给 OpenAPI 调用方。

3. **ModelRouter 返回 decision summary。** P0 不接完整 PolicyEngine；预算超限先返回 `fallback` 或 `policy_required` 的结构化 decision，后续 Phase 7 可把它接入 policy。
   - 替代方案：超预算直接抛异常。拒绝，因为 Product-Spec 要求 fallback / policy decision 可追踪。

4. **ContextAssembler 使用显式 fragment DTO。** history、retrieval、tool output、artifact ref 都先变成 `ContextFragment`，assembler 只做预算、裁剪、排序和 trace，不直接读取 tool/MCP/retrieval provider。
   - 替代方案：assembler 自己调用 retrieval/tool。拒绝，因为会提前侵入 Phase 8/9。

5. **Context assembly 与 embedding cache 必须有持久化记录。** `context_assemblies` 保存 input refs、token budget、trust summary、truncation summary 和 output_ref；`embedding_cache` 保存 input hash、provider、model、vector ref 与 cache metadata。local profile 用 SQLite，service profile 用 PostgreSQL 同一 repository contract。
   - 替代方案：只做 in-memory cache。拒绝，因为 DEV-PLAN 已把 `embedding_cache` 和 `context_assemblies` 定为 Phase 6 数据边界。

6. **delegation 归并先做可追踪摘要，不做完整 workflow 编排。** registry 校验 edge 后，Phase 6 记录 parent/child agent、run/trace/budget usage refs 的 `DelegationSummary`；真正的 policy 审批、复杂 graph 和跨 agent 调度策略留给后续 Phase。
   - 替代方案：只返回 allow/deny。拒绝，因为 Product-Spec 的 AC-016 要求 parent run 能归并 delegated usage、budget 和 trace。

## 影响表面

- 核心包：`agent_harness.registry`、`agent_harness.models`、`agent_harness.context`、`agent_harness.embeddings`、`agent_harness.adapters.models`。
- CLI：`agent-harness agents list` 子命令。
- service app：`templates/service-app/app/api/routes/agents.py`、app factory 依赖注入、runtime registry 构造。
- API 契约：`API-Contract.md` 的 `AGT-001` endpoint 条目和局部 OpenAPI drift tests。
- 模板配置：`templates/service-app/agents/examples/basic/config.yaml` 作为 registry smoke agent。
- 存储：新增 Alembic revision、SQLAlchemy models/repositories，覆盖 `context_assemblies` 与 `embedding_cache`。
- 质量门禁：import boundary 检查新增 provider SDK 允许列表。

## 测试接缝

- 模块 seam：`AgentRegistry.load_from_directory`、duplicate/invalid config、`check_delegation`。
- CLI seam：`uv run agent-harness agents list --agents-dir ...`。
- API seam：FastAPI `create_app(registry=...)` 后的 `/api/v1/agents` OpenAPI schema 和 error envelope。
- 模型 seam：`ModelRouter.route()` 在 fake provider、预算超阈值、fallback provider 下的 decision。
- 上下文 seam：`ContextAssembler.assemble()` 对多来源片段和 token budget 的 trace。
- 存储 seam：SQLite/PostgreSQL repository contract 覆盖 `context_assemblies` 与 `embedding_cache` 的写入、读取和 cache hit。
- Embedding seam：mock/local provider + 持久化 cache hit/miss metadata。
- 静态 seam：import boundary check 证明业务 agent/runtime core 不直接 import `pydantic_ai`。

## 风险 / 取舍

- [Risk] Phase 6 能力面较宽，容易提前实现 Phase 7/8/9。→ Mitigation：tasks 明确只做 registry/model/context/embedding seam，不实现 policy/tools/retrieval provider。
- [Risk] provider adapter 触碰外部 API 不稳定。→ Mitigation：优先依赖 lockfile/官方文档核实，Pydantic AI adapter 做薄边界，测试默认走 fake provider。
- [Risk] ContextAssembler 的 token 估算不是模型 tokenizer 级精确。→ Mitigation：P0 使用稳定估算和 trace，保留 provider tokenizer seam，验收看可解释降级而非精确计费。
- [Risk] 新增 migration 会放大验证成本。→ Mitigation：复用 Phase 3 repository/UoW 和现有 service smoke，local SQLite 与 PostgreSQL service profile 分开证明。

## 迁移计划

需要新增 Alembic revision，创建 `context_assemblies` 与 `embedding_cache` 表。实现按契约优先：先更新 `API-Contract.md` 和 contract tests，再加 migration/repository、registry/model/context/embedding 模块，最后接 CLI/API/smoke。回滚可通过删除新增表、删除新增 modules/routes/tests，并恢复 `API-Contract.md` 的 `AGT-001` 保留项。

## 开放问题

- 无阻塞问题。真实 provider key、policy 接入、retrieval index 和复杂 provider-native embedding workflow 均是后续 Phase 范围。
