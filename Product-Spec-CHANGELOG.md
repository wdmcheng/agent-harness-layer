# 变更记录

## [v1.4] - 2026-07-11
### 状态同步
- 根据 Phase 13 三个 active change、真实 PostgreSQL/Redis/DBOS/Compose 证据与 fresh review 结果，勾选 AC-059、AC-060、AC-062 及分进程部署边界完成项；仅同步验收状态，不修改需求语义。
- Phase 14 深度维护文档与 Phase 15 CI/release automation 验收继续保持未完成；三个 Phase 13 change 保持 active，等待用户决定是否归档。

## [v1.3] - 2026-07-10
### 状态同步
- 根据 Phase 12 三个归档 change、最终测试/审查证据与提交历史，勾选已完成的模板运行、OpenAPI、approval/tool、context/retrieval、trace/eval、四示例、README 和 vendor boundary 验收项；未修改需求语义。
- Phase 12.5 的 experiment/holdout/harness acceptance、Phase 13 的分进程，以及 Phase 14-15 的深度文档、CI 与 release 验收继续保持未完成。

## [v1.2] - 2026-07-10
### 调整
- 同步 Phase 12 `AgentExecutor` 契约：手工新增 agent 与 scaffold 生成路径都必须在 `agent.py` 暴露公共 protocol 入口，并在 `config.yaml` 声明 package-local executor reference。
- 明确 executor 缺失、越过 agent package、module/callable 无效或不符合 protocol 时 registry 整体拒绝，不允许回退到固定 fake output。
- 明确 executor reference 属于私有加载配置，不进入 public descriptor、API 或 CLI payload。
- 细化 `AC-013` approval continuation：approval-gated run 进入 waiting 并持久化 checkpoint/approval 后，必须能在进程重启、使用同一持久化 storage 重建 registry、executor resolver、orchestrator 和 approval service 的条件下，经私有 lease、绑定 `ApprovalGrant` 与 runtime 内部 resume 恢复原 continuation；公开 resume token 不构成执行授权。
- 明确公开 `RUN-005` 只恢复普通 checkpoint；approval-gated checkpoint 直接提交原始 token 必须在消费 token 或调用 handler 前返回稳定冲突，真实 approve 通过 `APR-002` 原子仲裁且仅在确定性结果和 run terminal 落库后公开为 approved。
- 补全 approval owner 硬退出恢复：raw claimed lease 只有在可配置 timeout 到期且不存在 execution claim时才能由真实 resolve 重试换发 fencing id；活跃 lease、已有 claim与旧 owner均不得被并发抢占或继续执行。

## [v1.1] - 2026-07-06
### 调整
- 同步最新架构图语义：补充 Agent Loop、HITL 回边、SSE/WS 流式回传、信任边界和 Prompt / 策略版本回溯要求。
- 明确 P0 InputGuardrail 契约：用户/API/CLI 输入进入 run 前执行轻量过滤、注入风险检测、trust marker 标注，并把检查结果写入 trace/audit。
- 明确 MCP tool output、tool output、retrieval chunk 默认作为 untrusted input 处理；进入模型上下文前必须保留 source_ref、trust_level、artifact_ref 和截断信息。
- 将 REQ-012 扩展为“模型、预算、上下文组装与 embedding”，新增 ContextAssembler 对 history、retrieval、tool output、artifact refs、token budget 和 fallback decision 的收口责任。
- 扩展 CanonicalEvent P0 事件类型，加入 input.guardrail.* 与 context.assembly.* 事件，并要求 local/jsonl 只记录摘要、来源、可信级别和截断元数据。
- 新增 GuardrailCheck 与 ContextAssembly 数据模型条目，补充上下文组装和信任边界的数据规则。

## [v1.0] - 2026-07-05
### 新增
- 新增 Agent Harness Layer 初始 Product Spec。
- 新增后端服务型 agent 脚手架产品定位，明确不是单一 demo agent、不是完整 SaaS 管理台。
- 新增 `uv workspace` monorepo、`packages/agent-harness` 可打包核心库、`templates/service-app` 后端模板范围。
- 新增 Pydantic AI 默认生态和 `agent_harness` 适配层策略，默认依赖上游包，不 vendoring 全源码。
- 新增 DBOS P0 durable execution、SQLite local checkpoint、Temporal P1 adapter 策略。
- 新增 SQLAlchemy 2.0 typed declarative + Alembic + Repository + Unit of Work 存储策略。
- 新增 PostgreSQL/Redis service profile 和 SQLite/filesystem local profile。
- 新增多 agent registry 与受控 delegation 规格。
- 新增默认租户、IdentityContext、API Key/Bearer Token 认证规格。
- 新增 PolicyEngine、危险动作可配置审批、HITL CLI/HTTP 审批和审计要求。
- 新增 FileTool、ShellTool、MCP client、RetrievalProvider、EmbeddingProvider、ModelRouter 能力。
- 新增 BM25 P0、PGroonga P0 optional adapter、pgvector P0 optional adapter、hybrid retrieval + RRF 接口。
- 新增 CanonicalEvent 事件模型、SSE/CLI/local-jsonl/OTel adapter 和事件 resume 规则。
- 新增 Observability 转换层，明确 local/jsonl 永久保留，Logfire 推荐，Phoenix/Langfuse 走 adapter。
- 新增 Eval Gate 转换层和 trace -> eval case -> human review -> approved dataset -> eval run -> score sink -> observability provider 闭环。
- 新增四个 P0 薄样例 agent：RAG assistant、ticket triage、repo analyst、dev assistant。
- 新增 README 和深度文档要求，明确目录结构树、职责和禁止跨边界规则必须写入 README。
- 新增 TDD 强约束、unit/contract/integration/eval/smoke 测试结构、ruff、pyright、pytest、coverage、pre-commit。
- 新增 GitHub Actions 与 GitLab CI 等价门禁。
- 新增 release automation / tag / CHANGELOG generation 为 P0，包括 SemVer、Conventional Commits、wheel/sdist artifact、私有发布路径。
- 新增 Apache-2.0 license、NOTICE、引用声明和 license check 要求。
- 新增 P0 未来微服务拆分基础要求：P0 不强制全量微服务化，但必须定义 API、runtime worker、model/tool gateway、storage、event/observability 的稳定边界和分进程 service profile 验收。
