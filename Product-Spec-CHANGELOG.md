# 变更记录

## [v1.10] - 2026-07-19
### Phase 13.9 归档

- 将 `sse-event-streaming` 的三个 delta specs 精确同步到 `canonical-events-artifacts`、`service-app-shell` 与新增的 `sse-event-streaming` 主规格，保留既有主规格内容。
- `sse-event-streaming` 已以 17/17 tasks 归档到 `openspec/changes/archive/2026-07-19-sse-event-streaming/`；当前无 active change，不代表 push、发布或部署。

## [v1.9] - 2026-07-19
### Phase 13.9 状态同步

- 根据 `sse-event-streaming` 实现与验证证据，完成 RUN-006 SSE transport、`Last-Event-ID` 恢复、CLI-EVT-001 canonical NDJSON、统一授权 EventSink reader 与首 frame P95 门禁；WebSocket、跨 run multiplex、外部 broker gateway 和 event retention 仍不在本次 P0 范围。
- 勾选 AC-017、AC-038、AC-066；真实 PostgreSQL/Redis service smoke 已覆盖 SSE 初始读取、exclusive resume、terminal EOF、非法第二 cursor 与零业务副作用，3 名 fresh reviewer 已完成 Stage 1/2，active change 保持未归档并进入 `ready-to-archive`。

## [v1.8] - 2026-07-18
### 规格维护

- 将既有 `budget.max_tokens_per_run` / `budget.max_cost_usd_per_run` 在 P0 预发布阶段收紧为 parent execution tree 共享硬上限：root direct model/embedding、获准 delegation 及 child allocation 统一竞争同一 durable owner ledger；公开字段与 `/api/v1` shape 不变，cost 为 `null` 时只关闭 shared cost 维度。
- 补齐 shared-budget 安全边界：tenant-scoped keyed request fingerprint 只能通过 typed settings 的 env / Docker secret file 边界加载，启动时 fail closed；任何 runtime、migration、evidence、错误或配置快照不得持久化或回显密钥原值。
- 补齐 `0016` 历史迁移合同：DDL 前校验全库 parent-child 拓扑，拒绝嵌套、孤儿、循环、跨租户或 delegation relation 不唯一；未封闭 tree 只能使用独立 durable immutable source evidence 回填，cost-enabled snapshot 的必需价格不得为 null。
- 统一 usage application UoW 错误优先级，并要求未封闭 shared-budget claim/allocation、unknown 或 needs-review 状态阻止 parent terminal；RUN-002 最终以 `RunDetailResponse` 为唯一合同，消除 active changes 归档投影冲突。

## [v1.7] - 2026-07-16
### 规格维护
- 收紧 Run API 公开契约：按实际 operation 区分 response status，RUN-002 原子切换为 `RunDetailResponse`，并要求 route、schema、OpenAPI 与双向 drift test 保持一致。
- 完成配置与 secret file 边界定义：四类 application startup 入口统一 fail closed；Docker secret file 受信根目录、普通文件、大小、direct/file 冲突和错误脱敏规则成为可执行验收，异常链及 traceback frame locals 不得泄漏原值。
- 固定 canonical run trace 与 evidence 关联：run 创建前生成唯一 trace，传播到 checkpoint/resume、worker、approval、CanonicalEvent、usage 与 delegation；历史 shape 通过前滚迁移收敛，event-id 重放必须核对除 sink seq 与重建 timestamp 外的完整稳定语义。
- 增加 model/embedding provider-neutral usage 契约：统一 started/final evidence、稳定语义调用槽位、token/cost/latency 校验、durable settlement/outbox、terminal 前恢复顺序和 local `<5s` 性能验收；embedding cache 也纳入同一证据边界。
- 增加真实受控 delegation 契约：明确 edge/policy/tenant/cycle/depth/budget/idempotency 前置门禁、parent 级原子预算与 event capacity 预约、`0015` migration、local/Redis worker 恢复、durable parent aggregation、RUN-002 detail 以及 unknown/非法 usage 的 needs_review 处理；RUN-002 对已持久化但尚未结算的 child 仍返回身份与活动状态，只有确实没有 child relation 时才返回 null，完成与活动 child 并存时不得遗漏且预算状态保持 incomplete。
- 校正 CanonicalEvent 固定目录为 39 种，纳入 `artifact.created` 和四种 delegation 生命周期事件；固定 delegation 的最多三条顺序、稳定 event id、parent run/trace/source agent 归属、internal/non-terminal 可见性、阶段 payload 与敏感字段禁止项。
- 强化 terminal 不变量为双向约束：只有 `run.completed|run.failed|run.cancelled` 可以且必须设置 `terminal=true` 和 `visibility=public`，其他事件必须 non-terminal；不一致 envelope 必须在 seq、容量、artifact 和 fan-out 副作用前拒绝。
- 更新 Phase 13.5、13.6、13.6A、13.7 与 13.8 的实施状态：已完成项保持 active 并只到 `ready-to-archive`，不代表归档、发布或部署；新增事件目录与 terminal 边界已由 AC-067 和 `agent-delegation-execution` task 1.3 固定，Phase 13.8 的真实委派、durable parent aggregation、恢复与幂等边界已通过完整门禁和最终代码 1+2；生产代码及历史大测试已按职责和行为域拆分，公开 facade、ORM 注册顺序、`typing.Literal` 身份和测试收集完整性均有回归证据。

## [v1.6] - 2026-07-12
### 基线审查修正
- 根据 Phase 1-13 基线审查和用户裁决，保留真实 delegation 与 SSE transport 的 P0 承诺；二者必须在 Phase 14/15 前通过聚焦 OpenSpec change 实现，当前保持未完成。
- 将 P0 secrets 范围收窄为 env / Docker secret file 配置消费与全链路脱敏；抽象 SecretProvider、Vault/KMS adapter 明确放入 P1，并新增可执行验收标准。
- 补充 model/embedding token、cost、latency trace 和性能 NFR 的可执行验收；这些行为缺口仍保持未完成，不以现有 DTO 或 JSON events seam冒充完成。
- 同步 17 个有直接实现与合同测试证据的 AC 状态；新增 RUN-006 后 AC-017 重新打开。AC-008 仅有 loader 证据；AC-050 改为当前可审计的 REQ/AC -> production -> test evidence 追踪，同时保留新 change 必须先有 red 证据的过程门禁。
- 明确 CI quality job 分别执行 `make quality` 与 `make test`，不再把 unit/contract tests 误写成 `make quality` 单命令的当前职责。
- 补齐 Phase 12.5 的 EvalDatasetSplit、EvalExperiment、HarnessAcceptance 数据实体，并明确持久化业务实体必须直接携带 tenant_id。
- 明确 Graph workflow 和 Redis session cache 为 P1 可选能力，修正架构 validator 的两个受控 crossing warning 记录；Phase 14、15 继续保持未完成。

## [v1.5] - 2026-07-12
### 状态同步
- 根据用户显式归档指令，将 Phase 13 三个已完成 change 按依赖顺序同步到长期主规格并归档；仅同步生命周期状态，不修改需求语义。
- 当前无 active OpenSpec change；Phase 14 深度维护文档与 Phase 15 CI/release automation 验收继续保持未完成。

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
