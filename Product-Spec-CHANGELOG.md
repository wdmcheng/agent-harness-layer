# 变更记录

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
