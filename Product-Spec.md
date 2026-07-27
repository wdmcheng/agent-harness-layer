# 产品需求规范：Agent Harness Layer

## 0. AI 使用说明

- 本文档是 Agent Harness Layer 的产品功能、范围、行为和验收标准的事实来源。
- AI MUST 优先实现 P0。
- AI MUST NOT 实现“不在本版本范围”中明确排除的内容。
- AI MUST 根据“验收标准”和“能力块验收矩阵”判断功能是否完成。
- 如果信息不明确，AI MUST 使用“假设”中的假设；如果仍无法判断，应记录到“待确认问题”，而不是自行扩展需求。
- 本项目是脚手架与核心库，不用单一 demo 的 MVP 成功体验验收；必须按能力块逐项验收。
- P0 不强制拆成微服务，但 MUST 按未来微服务拆分预留清晰边界；任何实现都不能把 API、runtime、tool、model、storage、event/observability 糊成不可拆单体。
- 本文档不使用、不引用任何泄漏源码。涉及 Claude Code 的判断只允许来自官方文档、公开 SDK 行为和可观察公开资料。

## 1. 产品上下文

### 1.1 产品摘要

Agent Harness Layer 是一个面向企业级后端服务型 agent 应用的 Python 脚手架与核心库。它以 Pydantic AI 生态为默认底座，但通过 `agent_harness` 适配层隔离上游变化；以五层运行架构、权限/HITL、观测/eval 闭环、TDD 门禁和可打包核心库为核心。

它不是单一 agent demo，不是完整 SaaS 管理台，也不是另起炉灶重写一个 agent 框架。它要解决的是：开发者如何用企业级后端工程方式持续开发、测试、观测、评估和发布多类型 agent 应用。

### 1.2 架构依据

项目内架构图为核心输入：

| 图 | 作用 | 可编辑源 | PNG 预览 |
|---|---|---|---|
| 企业级 Pydantic AI 控制论全栈架构 | 产品级全景图，定义 5 层运行链路、Agent Loop、治理面、观测面和 P0 可拆部署边界。 | `docs/architecture/pydantic-ai-agent-architecture.drawio` / `docs/architecture/pydantic-ai-agent-architecture.excalidraw` | `docs/architecture/pydantic-ai-agent-architecture.png` |
| 技术架构图（Agent Harness Layer） | 开发级结构图，落到核心包、template app、DTO、CanonicalEvent、Repository/UoW、config/identity/runtime/event/artifact/storage 边界。 | `docs/architecture/agent-harness-technical-architecture.drawio` / `docs/architecture/agent-harness-technical-architecture.excalidraw` | `docs/architecture/agent-harness-technical-architecture.png` |
| 运行链路与信任边界图（Agent Harness Layer） | 运行级链路图，说明 CLI/API -> RunOrchestrator -> storage/checkpoint -> EventBus/artifact -> JSON events/SSE，并标出 tool/MCP/retrieval 不可信输入边界。 | `docs/architecture/agent-harness-runtime-trust-boundaries.drawio` / `docs/architecture/agent-harness-runtime-trust-boundaries.excalidraw` | `docs/architecture/agent-harness-runtime-trust-boundaries.png` |
| 部署边界图（Agent Harness Layer） | 部署级边界图，说明 local profile、service profile、API/worker/PostgreSQL/Redis 协作，以及未来 gateway / worker pool / storage / event pipeline 拆分路径。 | `docs/architecture/agent-harness-deployment-boundaries.drawio` / `docs/architecture/agent-harness-deployment-boundaries.excalidraw` | `docs/architecture/agent-harness-deployment-boundaries.png` |

全景图重点表达：

- 纵向 5 层运行中轴：Access、Runtime、Engine、Tools、Infra。
- 目标主链路回边：当前 Runtime 通过稳定 orchestrator/adapter seam 驱动 Agent；P1 可在该 seam 后引入 Graph 节点。Engine 与 Tools 形成 Agent Loop；P0 已实现 SSE 向 Access 流式回传，WS 属于 P1 可选 adapter；HITL 审批回到 Runtime/Access 后 resume。
- Access / Tools / Retrieval 的信任边界：外部输入、MCP tool output、检索内容都必须先标记来源和可信级别，再进入上下文组装或执行路径。
- Engine 层上下文组装收口：历史裁剪、检索注入、工具结果截断、预算控制和异步记忆压缩不能散落在业务 agent 里。
- 左翼 Eval Gate：分层 eval、release gate、trace 低分样本回流。
- 右翼 Observability：OTel trace/metrics/audit，适配 Logfire / Phoenix / Langfuse。
- 底部工程闭环：线上低分 trace -> eval case -> eval run -> score -> observability provider；Prompt / 策略版本必须可回溯对比。

### 1.3 用户问题

目标用户有企业级 Java Web / 后端架构经验，但刚进入 agent 开发生态时会遇到这些问题：

- Pydantic AI 生态能力增长快，上游 API 和包边界变化风险需要隔离。
- 普通 agent 示例偏 demo，缺少企业级后端常见的分层、权限、审计、测试、发布和可观测边界。
- Eval Gate 和 Observability 经常并排存在，但没有形成 trace -> eval -> score -> trace 的工程闭环。
- 脚手架如果只服务单个 agent 示例，会失去扩展价值；如果一开始就造完整平台，会迟迟跑不起来。
- 权限、租户、HITL、event stream、storage、release automation 如果后补，会造成大范围重构。
- P0 如果只做进程内单体耦合，后续拆 API gateway、runtime worker、tool/model gateway 和数据服务会付出高昂重构成本。

### 1.4 目标用户

| 用户类型 | 描述 | 核心需求 |
|---|---|---|
| Agent 应用开发者 | 有后端工程经验，希望用 Python/Pydantic AI 开发后端服务型 agent | 快速得到结构清晰、可测试、可观测、可扩展的 agent 应用模板 |
| 脚手架维护者 | 维护 `agent_harness` 核心包、模板、adapter 和 CI/release 流程的人 | 保持核心包可打包、边界清楚、版本可治理、adapter 可替换 |
| 企业技术负责人 | 评估 agent 应用是否具备工程化落地条件的人 | 看到权限、审计、eval、观测、发布、依赖治理和 TDD 门禁 |

### 1.5 核心价值

- 把企业级后端工程纪律迁移到 agent 应用开发。
- 用 `agent_harness` 隔离 Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 等上游实现。
- 用能力块矩阵保证脚手架不是空目录树，而是每一块都可测试、可运行、可观测。
- 用 local profile 降低上手成本，用 service profile 支持后端服务化部署。
- 用 release automation 从 P0 开始约束可打包、可发布、可升级的核心库边界。

### 1.6 成功标准

| 判断标准 | 目标 / 信号 |
|---|---|
| 包边界成立 | `packages/agent-harness` 可独立 build wheel，并可被模板以 wheel/path dependency 安装使用 |
| 模板可用 | `templates/service-app` 可在 local profile 和 service profile 下跑通核心流程 |
| 能力块可验收 | runtime、storage、policy、tools、events、observability、eval、retrieval、release 都有测试和验收证据 |
| 闭环成立 | failed/low-score trace 可生成 draft eval case，经人工确认后进入 approved dataset，eval score 可写回本地 sink 和可配置观测 provider |
| 闭环可优化 | 示例 agent 提供真实行为分布后，eval case 可按行为标签拆分 optimization / holdout，baseline 与候选 harness 可对比，回归和人工验收共同决定是否接受 harness 变更 |
| 上游可替换 | 业务 agent 不直接 import Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 等厂商实现 |
| TDD 落地 | 每个能力块开发前有失败测试或 contract test，完成后测试转绿并进入 CI |
| 发布可治理 | P0 内置版本、tag、CHANGELOG、build、release artifact、私有发布路径和 GitHub/GitLab CI 配置 |

## 2. 范围

### 2.1 本版本范围

| 编号 | 内容 | 优先级 | 备注 |
|---|---|---|---|
| SCOPE-001 | `uv workspace` monorepo 结构 | P0 | 分离核心包、模板、示例、文档 |
| SCOPE-002 | `packages/agent-harness` 可打包核心库 | P0 | 支持 wheel/sdist、本地安装、私有发布 |
| SCOPE-003 | `templates/service-app` 后端服务型 agent 模板 | P0 | FastAPI + CLI + worker + configs + tests |
| SCOPE-004 | Pydantic AI 默认生态与适配层 | P0 | Pydantic AI core 是默认 runtime；`pydantic-ai-harness` 是可选 capability library，按能力需要经 integration boundary 引入，不作为 P0 必选依赖 |
| SCOPE-005 | DBOS durable execution 默认 adapter | P0 | service profile 默认，local profile 用 SQLite-backed checkpoint |
| SCOPE-006 | SQLAlchemy 2.0 + Alembic storage | P0 | PostgreSQL/SQLite adapter，Repository + Unit of Work |
| SCOPE-007 | PostgreSQL + Redis service profile | P0 | Redis 默认带上，但核心抽象不硬绑 Redis |
| SCOPE-008 | SQLite + filesystem local profile | P0 | 本地开发、CI、离线测试可完整跑 |
| SCOPE-009 | 多 agent registry 与受控 delegation | P0 | 支持多个 agent 注册、路由、隔离、受控互调 |
| SCOPE-010 | 轻量租户与身份上下文 | P0 | 永远有 `tenant_id`，单租户使用默认租户 |
| SCOPE-011 | API Key / Bearer Token 认证 | P0 | 注入 `IdentityContext`，不做登录/注册 UI |
| SCOPE-012 | PolicyEngine 与权限拦截内核 | P0 | 做策略接口、拦截点、审计，不做权限管理后台 |
| SCOPE-013 | HITL 审批协议和 CLI/HTTP 入口 | P0 | 危险动作可配置 approval，支持 checkpoint/resume |
| SCOPE-014 | Workspace file tools | P0 | read/write/list/search/patch/delete，受 workspace 和 policy 限制 |
| SCOPE-015 | Shell tool | P0 | 默认关闭，显式启用，强管控和审计 |
| SCOPE-016 | MCP client connector | P0 | stdio + HTTP/SSE、tool discovery、allowlist、policy |
| SCOPE-017 | RetrievalProvider + BM25 + optional PGroonga/pgvector adapter | P0 | BM25 必备，PGroonga/pgvector 是 P0 可选 adapter |
| SCOPE-018 | EmbeddingProvider | P0 | OpenAI-compatible、mock/local、cache、trace |
| SCOPE-019 | CanonicalEvent 事件模型 | P0 | SSE、CLI、local/jsonl、OTel adapter |
| SCOPE-020 | Observability 转换层 | P0 | OTel 底座，local/jsonl 永久保留，Logfire 推荐 |
| SCOPE-021 | Eval Gate 转换层与 trace/eval 闭环 | P0 | draft -> human review -> approved -> eval -> score sink；四个示例 agent 完成后补齐 behavior tags、optimization/holdout split、baseline/compare 和 human acceptance gate |
| SCOPE-022 | README 与深度文档 | P0 | 面向 app developer 和 scaffold maintainer 两类读者 |
| SCOPE-023 | TDD 测试结构与质量门禁 | P0 | unit、contract、integration、eval、smoke |
| SCOPE-024 | GitHub Actions + GitLab CI | P0 | 两边跑同一命令集，包含 fake model eval 和 service smoke |
| SCOPE-025 | Release automation / tag / CHANGELOG generation | P0 | SemVer、Conventional Commits、自动版本、tag、release artifact、CHANGELOG |
| SCOPE-026 | Apache-2.0 license 与开源合规 | P0 | LICENSE、NOTICE、引用声明、license check |
| SCOPE-027 | CLI | P0 | Typer，包含 run、doctor、approval、eval、policy、scaffold agent |
| SCOPE-028 | 示例 agent 薄样例 | P0 | RAG、ticket triage、repo analyst、dev assistant，各自验证扩展点 |
| SCOPE-029 | 未来微服务拆分基础 | P0 | P0 不强制微服务部署，但必须定义模块/进程/接口边界，避免后续拆分重构 |
| SCOPE-030 | 架构治理与持续演进基线 | P0 | 统一人与 Agent 使用的分层、依赖、模式选择、代码规范、长期计划和可执行架构门禁；按窄 change 演进，不做大爆炸重构 |
| SCOPE-031 | 受控真实文本模型运行入口 | P0 | 在部署允许列表、Agent 冻结策略和单次请求意图的交集内调用真实模型；密钥、endpoint、预算、重试、并发和证据均由类型化配置与运行时边界控制 |
| SCOPE-032 | 受控真实模型增量文本流 | P0 | 在 SCOPE-031 的冻结 route、预算和 provider 生命周期上生产 provider-neutral 文本增量，经既有 CanonicalEvent / SSE / CLI 传输；断线、取消、部分计量、背压和恢复不得触发重复 provider 副作用 |

### 2.2 不在本版本范围

| 编号 | 内容 | 原因 |
|---|---|---|
| OUT-001 | 完整 SaaS 管理后台 | 本项目是脚手架，不是产品化后台系统 |
| OUT-002 | 用户注册、登录页、组织邀请、计费 | P0 只做认证和身份上下文，不做 SaaS 产品能力 |
| OUT-003 | RBAC/ABAC 图形化配置后台 | P0 做 policy 内核和 provider，不做管理 UI |
| OUT-004 | 公开 PyPI 强制发布 | P0 必须可打包、可私有发布；公开发布可后续决定 |
| OUT-005 | Pydantic AI 源码 vendoring / 长期私有分支 | 上游默认包依赖，只有关键 bug/合规/上游不收能力才 fork/patch |
| OUT-006 | 复杂 multi-agent graph/workflow 编排 UI | P0 支持 registry/delegation，复杂编排放 P1 |
| OUT-007 | MCP server 开发框架 | P0 做 MCP client，server template 放 P1 |
| OUT-008 | 产品化前端 UI | P0 只提供 API、CLI、OpenAPI/Swagger/Redoc |
| OUT-009 | OIDC/OAuth2 | P0 API Key/Bearer Token，OIDC/OAuth2 adapter 放 P1 |
| OUT-010 | SecretProvider、Vault/KMS 等密钥管理 adapter | P0 只消费 env / Docker secret file 注入并执行脱敏；抽象 SecretProvider 与 Vault/KMS adapter 放 P1 |
| OUT-011 | OpenSearch / Elasticsearch / Vespa adapter | P0 留 RetrievalProvider，搜索集群 adapter 放 P1/P2 |
| OUT-012 | AG-UI / Vercel AI stream adapter | P0 先做 CanonicalEvent + SSE/CLI/OTel/local-jsonl |
| OUT-013 | 自动 eval case 入库默认开启 | P0 必须人工确认；规则自动入库放 P1 且默认关闭 |
| OUT-014 | P0 强制全量微服务拆分 | P0 先做可拆边界和 service profile，物理微服务化放后续演进 |

## 3. 用户任务

| 编号 | 用户任务 | 用户类型 | 优先级 |
|---|---|---|---|
| TASK-001 | 开发者创建并运行一个后端服务型 agent 应用 | Agent 应用开发者 | P0 |
| TASK-002 | 开发者在模板中新增一个 agent | Agent 应用开发者 | P0 |
| TASK-003 | 开发者为 agent 添加工具、权限策略和预算 | Agent 应用开发者 | P0 |
| TASK-004 | 开发者用 local profile 本地运行、测试、eval 和调试 | Agent 应用开发者 | P0 |
| TASK-005 | 开发者用 service profile 连接 PostgreSQL/Redis 并跑 smoke | Agent 应用开发者 | P0 |
| TASK-006 | 开发者处理危险动作审批并恢复 agent run | Agent 应用开发者 | P0 |
| TASK-007 | 开发者从 failed/low-score trace 生成并审核 eval case | Agent 应用开发者 | P0 |
| TASK-008 | 维护者扩展 observability/eval/storage/runtime adapter | 脚手架维护者 | P0 |
| TASK-009 | 维护者按 TDD 开发核心能力块并通过 CI | 脚手架维护者 | P0 |
| TASK-010 | 维护者生成版本、tag、CHANGELOG 和 release artifact | 脚手架维护者 | P0 |
| TASK-011 | 企业技术负责人审查脚手架边界、合规、测试和发布证据 | 企业技术负责人 | P0 |
| TASK-012 | 维护者按共享架构原则、变更矩阵和可执行门禁逐步演进项目 | 脚手架维护者 | P0 |
| TASK-013 | 开发者配置受控真实模型部署且不把凭据写入版本库或运行证据 | Agent 应用开发者 | P0 |
| TASK-014 | 开发者通过既有事件入口消费真实模型增量文本，并在断线后只续读已持久化片段 | Agent 应用开发者 | P0 |

## 4. 用户流程

### FLOW-001: 使用模板启动本地 agent 应用

**关联任务：** TASK-001, TASK-004  
**优先级：** P0
**目标：** 开发者能在不注册 SaaS、不配置真实模型 key 的情况下跑通本地模板、测试、eval 和 trace。

**入口：**  
clone 仓库或复制 `templates/service-app`。

**主路径：**
1. 开发者执行 `uv sync`。
2. 开发者复制 `.env.example` 为 `.env`。
3. 开发者选择 `local` profile。
4. 开发者执行 `make dev` 或 `agent-harness run <agent_id>`。
5. 系统使用 SQLite + filesystem + local/jsonl + fake/mock provider 跑通示例 agent。
6. 开发者执行 `make test`、`make eval`、`make smoke-local`。

**分支路径：**
- 如果未配置真实模型，系统使用 fake model 和 mock embedding 跑测试/eval。
- 如果 local storage 初始化缺失，`agent-harness doctor` 给出修复建议。

**边界情况：**
- `.env` 缺失时必须提示复制 `.env.example`。
- SQLite 文件不可写时必须失败并给出路径和权限信息。
- local/jsonl sink 不可写时不能静默丢事件。

**完成状态：**
本地 agent run 有 terminal event，local/jsonl 产出 trace/eval/audit 记录，测试和 eval 可跑通。

### FLOW-002: 新增一个 agent

**关联任务：** TASK-002  
**优先级：** P0  
**目标：** 开发者按统一目录和配置新增 agent，不破坏核心包边界。

**入口：**  
`agent-harness scaffold agent <agent_id>` 或手动创建 `agents/<agent_id>/`。

**主路径：**
1. 开发者创建 `agent.py`、`tools.py`、`schemas.py`、`config.yaml`、`evals/`，并在 `agent.py` 暴露实现公共 `AgentExecutor` protocol 的入口。
2. `config.yaml` 声明 `agent_id`、package-local executor reference、模型策略、预算、工具白名单、eval dataset、delegation edge。
3. `AgentRegistry` 校验 descriptor、schema refs 和 executor reference；executor 只能解析到 config 所属 agent package 内的受控入口。
4. 开发者通过 CLI 或 `/api/v1/agents/{agent_id}/runs` 运行 agent。
5. 系统为 run 注入 tenant、identity、policy、budget、trace 和 event stream。

**分支路径：**
- agent config schema 不合法时 registry 拒绝加载。
- executor 缺失、越过 agent package、module/callable 不存在或不符合 protocol 时 registry 整体拒绝加载，不回退到固定 fake output。
- agent 未声明工具权限时 tool call 被 policy 拒绝或要求审批。

**边界情况：**
- `agents/*` 不允许直接 import 厂商 adapter。
- executor reference 不允许使用绝对路径、越过所属 agent package，且不得进入 public descriptor/API/CLI payload。
- 重复 `agent_id` 必须失败。
- delegation edge 未声明时 agent 互调必须拒绝。

**完成状态：**
新增 agent 可被 registry 列出、运行、测试、eval，并产生 trace/audit。

### FLOW-003: 危险动作审批与恢复

**关联任务：** TASK-003, TASK-006  
**优先级：** P0  
**目标：** 危险动作不被写死在代码里，而由可配置 policy 决定 allow/deny/require_approval。

**入口：**  
agent 运行中触发 shell、文件删除、workspace 外访问、外部网络/MCP、写 approved dataset、修改 policy 等动作。

**主路径：**
1. tool/runtime 调用 `PolicyEngine`。
2. `PolicyEngine` 返回 `require_approval`。
3. runtime 产生 `approval.required` 事件并创建 checkpoint。
4. CLI 或 HTTP 客户端提交 approve/deny；approve 原子取得不公开的 owner lease 和 fencing id，deny 与 approve 通过同一 repository 条件更新仲裁。
5. runtime 通过绑定 approval/checkpoint 上下文的 grant 恢复原 continuation，并在调用危险动作前于同一事务校验当前 fencing id、创建唯一 execution claim。
6. 确定性结果持久化后，把 `approval.resolved` 与 run terminal 写入同一 durable evidence outbox；按稳定顺序先发布 resolution evidence、最后发布 terminal，二者都确认后才公开完成 approval resolution。丢失确认只能按稳定 event id 重放 outbox，不能重放危险动作。

**分支路径：**
- deny 时 run 按策略失败或走 fallback。
- 进程只提交 raw claimed lease 后硬退出时，后续真实 resolve 只有在 owner timeout 到期且尚无 execution claim时才能原子换发 fencing id并继续；活跃 owner、已有 claim和旧 fencing id不得被抢占或继续执行。
- execution claim 已存在但缺少确定性结果时进入私有 needs-review，不自动重放外部副作用。
- 审批超时策略 P1；P0 可保持等待或由用户取消。

**边界情况：**
- 审批记录必须关联 `tenant_id`、`agent_id`、`run_id`、`trace_id`。
- 修改权限策略本身默认 `require_approval`。

**完成状态：**
危险动作不会绕过 policy；审批后 run 可跨进程重启恢复，raw lease owner 硬退出可在超时后安全接管，旧 owner 被 fencing，外部副作用通过唯一 claim 保持 at-most-once，审计链完整。

### FLOW-004: Trace 到 Eval 的闭环

**关联任务：** TASK-007  
**优先级：** P0  
**目标：** 线上 failed/low-score run 能变成受控 eval case，并把 eval score 写回观测后端。

**入口：**  
run failed、score 低于阈值、人工标记或 CLI 执行 `agent-harness eval draft`。

**主路径：**
1. `TraceCollector` 收集 run trace、tool I/O 摘要、model usage、artifact refs。
2. `Failure / Low-score Detector` 生成 `EvalCaseDraft`。
3. draft 写入 local review queue 或 repository。
4. 人通过 CLI/API 审核并 approve。
5. approved case 进入 approved dataset。
6. `EvalRunner` 跑 approved cases。
7. `ScoreSink` 将分数写入 local/jsonl，并通过 adapter 写回 Logfire/Phoenix/Langfuse 等 provider。

**分支路径：**
- 发现 secret 或隐私字段时必须脱敏或阻止入库。
- provider 未配置时 local/jsonl 仍保留完整本地证据。

**边界情况：**
- P0 不允许自动写入 approved dataset。
- 大 payload 必须用 artifact/ref，不进入 eval case 正文。

**完成状态：**
至少一个 failed/low-score trace 可生成 draft，经人工确认后被 eval runner 消费，并产出 score sink 记录。

**后续实验路径：**
四个 P0 示例 agent 提供真实行为分布后，系统必须把 approved eval cases 进一步组织成可优化的数据集：按行为标签分桶，拆分 optimization / holdout，先跑 baseline，再比较候选 harness 版本。候选 harness 只有在目标标签分数提升、关键回归受控、holdout 未明显退化且人工 review 通过后，才能被接受为下一版 harness。

### FLOW-005: 核心包发布自动化

**关联任务：** TASK-010  
**优先级：** P0  
**目标：** `agent-harness` 从 P0 就具备可发布包边界、版本、tag、CHANGELOG 和 release artifact。

**入口：**  
main 分支合并 Conventional Commits，或维护者手动触发 release workflow。

**主路径：**
1. CI 验证 `make quality`、`make eval`、`make smoke-local`、`make smoke-service`。
2. release automation 按 Conventional Commits 计算 SemVer 版本。
3. 系统更新 package version 和 `CHANGELOG.md`。
4. 系统创建 git tag 和 release notes。
5. 系统执行 `uv build` 生成 wheel/sdist。
6. 系统将 release artifacts 上传到 CI artifacts，并支持发布到私有 package registry。

**分支路径：**
- 无 releasable commit 时不发版。
- GitHub 和 GitLab 均必须有等价 release path；可以用不同底层 action/job，但命令和产物一致。

**边界情况：**
- release job 必须在质量门禁通过后才能运行。
- release automation 不得绕过 Apache-2.0、NOTICE、license check。

**完成状态：**
核心包能自动生成版本、tag、CHANGELOG、wheel/sdist 和 release artifact；模板声明与当前项目版本精确匹配的 `agent-harness` 自依赖。

### FLOW-006: 配置并调用受控真实文本模型

**关联任务：** TASK-003, TASK-004, TASK-008, TASK-013
**优先级：** P0
**目标：** 开发者可以在不泄漏凭据、不绕过 Agent 模型策略和预算门禁的前提下，为一个 Agent 启用真实文本模型；local/fake 路径仍保持离线可用。

**入口：**
部署维护者先在 profile 定义模型 deployment 的非敏感配置和凭据引用，再通过本机 `.env`、进程环境变量或受控 secret file 注入对应秘密值；Agent descriptor 只能从部署允许范围内进一步收窄模型策略。

**主路径：**
1. 配置加载器按公开优先级合并 profile、Agent 配置、本机 `.env`、secret file、进程环境和受控显式 overrides，并在启动阶段完成 schema、凭据引用和 endpoint policy 校验。
2. runtime 把部署允许列表、Agent 冻结模型策略和本次请求意图求交集，形成不可变 route plan；请求只能收窄，不能扩大部署或 Agent 权限。
3. route plan 在预算预约、授权成功审计或 provider 网络调用之前固定 deployment、provider、model、endpoint origin、能力、timeout、retry、并发和价格身份；校验失败仍可写入不含秘密的本地拒绝事件与审计，但不得把它表示为路由已获授权。
4. provider factory 从类型化凭据引用取得秘密值，在 adapter 边界创建客户端并执行真实文本调用；业务 Agent 不读取环境变量、不构造厂商 SDK 客户端。
5. 调用结果以 provider-neutral response、usage、latency、attempt 和 route identity 进入既有预算、事件、trace 与 audit 链路，秘密值不得进入任何证据。

**分支路径：**
- 未配置真实 deployment 或未显式选择时继续使用 local/fake，不自动探测宿主机上的厂商环境变量。
- 缺失凭据、未知 deployment/model、能力不匹配、endpoint 不受信、direct env 与 `_FILE` 冲突，或启用 cost hard limit 却缺少可验证价格时，必须在 provider 副作用前 fail closed。
- provider 瞬时失败只能按冻结的有限 retry policy 重试；超过总 deadline、并发舱壁或重试上限后返回结构化失败并保留不含 secret 的证据。

**边界情况：**
- `.env` 只解析 `AGENT_HARNESS_*` 键；把 `OPENAI_API_KEY` 等 provider 原生变量仅写入该文件，不构成 Harness 已加载凭据。
- `base_url` 通常不是秘密，但它决定凭据发往何处；含 userinfo、query、fragment、未批准 scheme/host 或未经显式允许的非 TLS endpoint 必须拒绝。
- 首个实现范围只覆盖非流式真实文本模型；增量文本流由紧随其后的 P0 Phase 18.1 `controlled-model-streaming` 独立交付，不能混入首个入口，也不能无限期留在未排序的“以后”。Provider-neutral structured output、模型驱动工具循环和多 provider 运行治理继续分别使用后续窄 change。

**完成状态：**
受控真实文本模型入口尚未实现；`PydanticAIModelProvider` 适配器已存在，但当前 service composition 仍拒绝非 `fake` provider，配置 schema 也尚未覆盖完整 deployment、endpoint 与凭据引用。现有 RUN-006 只读取已持久化的 `CanonicalEvent`，当前 provider/invocation 也尚未生产真实模型文本增量。

### FLOW-007: 通过既有事件入口消费受控真实模型增量文本

**关联任务：** TASK-004, TASK-008, TASK-014
**优先级：** P0
**目标：** 开发者在不新增第二条流式通道、不泄漏 provider SDK 事件且不破坏预算和恢复语义的前提下，逐段收到真实模型文本，并能在传输断线后继续读取已提交事件。

**入口：**
Phase 18 的受控真实文本模型 deployment 已验收并显式声明文本流能力；调用方显式选择流式文本运行，再通过既有 RUN-006 SSE 或 CLI event stream 消费事件。

**主路径：**
1. runtime 复用 Phase 18 已冻结的 route plan、预算预约、attempt identity 与 provider client，不由 SSE reader 重新选择或调用模型。
2. adapter 把 provider 增量归一化为只追加的文本片段；runtime 按稳定 operation、attempt 与 chunk 顺序形成有界 `model.output.delta`，先持久化 `CanonicalEvent`，再由既有 EventSink reader 输出。
3. 文本完成后，runtime 分别提交 `model.output.completed`、可验证的 usage/cost settlement 和 run terminal；partial delta 不冒充 final result，provider 原始事件不越过 adapter。
4. SSE 断线只停止本次读取，不默认取消 durable run；客户端以 `Last-Event-ID` 续读时只返回尚未读取的已持久化事件，绝不重启或重放 provider stream。

**分支路径：**
- 显式 run cancellation 或 provider deadline 会向 adapter 传播取消；如果 provider 不能证明已经停止，attempt 进入 interrupted/unknown，已观察文本和已知 usage 保留，未知费用不得记为零，也不得自动重试。
- 慢消费者或 storage/event sink 跟不上 provider 时使用冻结的有界分片、合并和背压策略；不得静默丢片、乱序、无限缓存或绕过 event capacity reservation。

**边界情况：**
- Phase 18.1 只覆盖普通文本增量，不覆盖 structured delta、reasoning 暴露、tool-call delta、模型工具循环或 AG-UI / Vercel AI adapter。
- 既有“已建立 SSE 连接且已有可见事件时首 frame 小于 1 秒”只衡量传输层；provider 首 delta、首个已提交 model delta 和客户端收到该 delta 必须分开记录，不能把外部 provider 时延伪装成 RUN-006 回归。

**完成状态：**
尚未实现。当前 CanonicalEvent 类型目录虽已预留 `model.output.delta` / `model.output.completed`，但这只定义合法事件词汇，不证明 provider token/text streaming、持久化顺序、容量预约或部分计量已经存在。

## 5. 功能需求

### REQ-001: Monorepo / uv workspace 结构

**优先级：** P0  
**关联任务：** TASK-001, TASK-008, TASK-010  
**关联流程：** FLOW-001, FLOW-005

**用途：**
保证核心包、模板、示例和文档边界清楚，避免 P0 之后再痛苦拆包。

**行为：**  
仓库 MUST 使用 `uv workspace` 管理以下边界：

```text
project/
├── packages/
│   └── agent-harness/
├── templates/
│   └── service-app/
├── examples/
├── docs/
├── scripts/
├── .github/workflows/ci.yml
├── .gitlab-ci.yml
├── pyproject.toml
├── uv.lock
├── LICENSE
├── NOTICE
├── CHANGELOG.md
└── README.md
```

**规则：**
- MUST `packages/agent-harness` 不依赖 `templates/*` 或 `examples/*`。
- MUST `templates/service-app` 通过 workspace/path dependency 或 wheel 依赖 `agent-harness`。
- MUST CI 验证 wheel 安装后模板仍可运行。
- MUST README 解释目录树、职责和禁止跨边界规则。

**验收标准：**
- [x] AC-001: Given 仓库根目录, when 执行 `uv sync`, then workspace 所有 P0 package 可以解析依赖。
- [x] AC-002: Given `packages/agent-harness`, when 执行 `uv build`, then 生成 wheel/sdist。
- [x] AC-003: Given 已生成 wheel, when 模板 app 使用 wheel 安装, then tests/smoke 不依赖源码路径也能通过。

### REQ-002: `agent_harness` 核心包与上游隔离

**优先级：** P0  
**关联任务：** TASK-002, TASK-008  
**关联流程：** FLOW-002

**用途：**  
隔离 Pydantic AI、DBOS、Logfire、Phoenix、Langfuse、Temporal 等上游变化。

**行为：**  
`packages/agent-harness/src/agent_harness/` MUST 提供稳定内部接口：

- `runtime/`
- `registry/`
- `models/`
- `tools/`
- `policy/`
- `observability/`
- `evals/`
- `storage/`
- `config/`
- `adapters/`

**规则：**
- MUST 业务 agent 只能依赖 `agent_harness` 公共接口。
- MUST 厂商依赖只出现在 `adapters/` 或受控 integration 模块。
- MUST Pydantic AI 作为默认底层实现，不 vendoring 全源码。
- SHOULD 记录 fork/patch overlay 的触发条件：关键 bug 上游未修、企业内必需能力上游不收、安全/合规必须控制源码。

**验收标准：**
- [x] AC-004: Given `agents/examples/*`, when 静态扫描 import, then 不出现直接 import `pydantic_ai`、`logfire`、`dbos`、`langfuse`、`phoenix`。
- [x] AC-005: Given 上游 adapter 被 fake adapter 替换, when 运行 unit/contract tests, then 核心接口测试仍可通过。

### REQ-003: 后端服务型模板

**优先级：** P0  
**关联任务：** TASK-001  
**关联流程：** FLOW-001

**用途：**  
提供可复制、可运行、可测试的后端服务型 agent 应用模板。

**行为：**  
`templates/service-app` MUST 包含：

```text
templates/service-app/
├── app/
│   ├── api/
│   ├── static/api-docs/
│   ├── cli/
│   └── workers/
├── agents/
│   └── examples/
├── configs/
│   └── profiles/
├── eval-cases/
│   ├── drafts/
│   └── approved/
├── tests/
├── docs/
├── scripts/update_api_docs_assets.py
├── docker-compose.yml
├── pyproject.toml
├── Makefile
├── .env.example
└── README.md
```

**规则：**
- MUST `app/*` 只做入口编排，不写业务 agent 逻辑。
- MUST `agents/*` 每个子目录是一个 agent。
- MUST `configs/profiles/local.yaml` 和 `configs/profiles/service.yaml` 都存在。
- MUST README 面向 app developer 和 scaffold maintainer 两类读者。
- MUST Swagger UI 和 Redoc 默认只从当前 service-app 加载固定版本的本地静态资源，保证复制项目在无外网环境也可使用；只有类型化 profile 或 `AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE` 显式切换为 `online` 时，才允许加载与本地资源相同锁定版本的 CDN 资源。
- MUST API 文档公开面可通过类型化 `service.api_docs.enabled` 或 `AGENT_HARNESS_SERVICE__API_DOCS__ENABLED` 整体关闭；`local` profile 默认开启，面向正式部署的 `service` profile 默认关闭。关闭时不得注册 `/docs`、`/redoc`、`/openapi.json`、Swagger OAuth2 redirect 或 `/static/api-docs` mount，也不得读取或校验文档静态资源。
- MUST 随模板提供可复现的 API 文档资源更新入口，同步记录 Swagger UI / Redoc 版本、来源、SHA-256 和许可证文件；下载或校验失败时不得留下部分更新。
- MUST 所有 `make eval*` 入口在进入 fail-closed runtime 前显式迁移各自的独立数据库，全新 `STATE_DIR` 不需要用户手动补迁移。

**验收标准：**
- [x] AC-006: Given 复制后的模板目录, when 执行 `make dev` with local profile, then health、OpenAPI、Swagger UI 和 Redoc 入口均可用，文档 UI 默认只请求本地资源，显式切为 `online` 后才请求与本地锁定版本一致的 CDN 资源。
- [x] AC-070: Given `service.api_docs.enabled=false`, when service-app 启动, then `/docs`、`/redoc`、`/openapi.json`、Swagger OAuth2 redirect 和 `/static/api-docs/*` 均返回 404，且缺失或损坏的文档静态资源不阻断应用启动；`local` 默认开启、`service` 默认关闭，并可由类型化环境变量显式覆盖。
- [x] AC-007: Given 模板目录, when 执行 `make smoke-service`, then Docker Compose PostgreSQL/Redis service smoke 通过。

### REQ-004: 配置系统

**优先级：** P0  
**关联任务：** TASK-001, TASK-002, TASK-003  

**用途：**  
统一 `.env`、YAML 和 typed settings，避免业务代码手读配置。

**行为：**
- `configs/profiles/*.yaml` 放可提交的环境 profile、provider deployment、endpoint policy、storage、observability、policy 等非敏感默认值和 secret 引用；不得保存密钥原值。
- `.env` 是被版本库忽略的本机覆盖层，可以为本地开发注入 `AGENT_HARNESS_*` 类型化配置和秘密值；它的优先级高于 YAML，但不是 secret manager，也不因文件名不同而天然比 YAML 安全。
- service profile 的秘密值从进程环境变量或只读 Docker secret file 注入同一 typed settings 字段；提交的 `.env.example` 只写变量名、占位符和安全说明。
- `agents/*/config.yaml` 放 agent 元数据、package-local executor reference、预算、工具白名单、eval dataset、delegation edge。
- `agent_harness.config` 负责加载、合并、校验。
- 公共配置优先级固定为 profile YAML → Agent YAML（仅进入 `agent` 子树）→ `.env` → secret file → direct 进程环境 → 受控显式 overrides；后者覆盖前者，但 direct env 与对应 `_FILE` 同时出现时必须在应用任何覆盖前失败。
- `.env` 和进程配置只把 `AGENT_HARNESS_*` 转成 `HarnessSettings`；provider 原生变量（例如 `OPENAI_API_KEY`）不得成为 Harness 内部不可见的第二条配置路径。真实 provider 必须由 composition root 通过类型化 credential reference 显式构造。

**规则：**
- MUST 所有配置有 Pydantic schema 校验。
- MUST 业务 agent 不直接 `open("config.yaml")`。
- MUST 业务 agent、runtime 和 migration 不直接读取 provider 原生环境变量或 secret 文件；只有配置加载边界解析 secret，composition root 只能取得类型化 secret 值或受控凭据对象。
- MUST profile YAML 与 Agent YAML 不保存 API key、token、password、client secret 等秘密原值；Agent YAML 只能收窄 deployment/model/capability 允许范围，不得覆盖部署 endpoint、credential reference 或扩大允许列表。
- MUST 本机 `.env` 保持在版本库忽略范围内；实现和文档不得把 `.env` 描述成生产 secret store。正式部署优先使用进程 secret 注入或受控 `_FILE`，并保持 direct/file 冲突 fail closed。
- MUST 配置校验错误包含字段路径和修复提示。
- MUST Docker secret file 只通过受控配置加载边界读取，拒绝目录、符号链接逃逸、不可读文件和空值；错误不得回显 secret 内容。
- MUST P0 不引入只有单一实现的 `SecretProvider` 抽象；env、`.env` 与 Docker secret file 在同一 typed settings 合并边界收口，并在日志、错误、trace、eval 和 audit 前脱敏。
- MUST shared-budget tenant-scoped request fingerprint 的 keyed secret 是 typed settings 字段，只能通过同一 env / Docker secret file 合并边界加载；缺失、direct/file 冲突、越界、symlink、超限、非 UTF-8 或空值必须在 application startup 失败，runtime 与 migration 不得自行读取环境变量或文件。
- MUST 每个 agent config 显式声明受控 executor reference；缺失、越界或无效 executor 必须让 registry 整体失败，不得隐式 fallback。
- SHOULD 配置加载边界为后续热更新保留 seam；P0 不要求 worker 运行中自动热重载，模型路由、预算和 provider 变更先走显式 reload / restart 路径。

**验收标准：**
- [x] AC-008: Given 缺失必填配置, when 启动应用, then 启动失败并输出 schema 错误。
- [x] AC-009: Given local/service profile, when 加载 settings, then storage、queue、observability、policy 解析到 typed config。
- [x] AC-063: Given service profile 将 secret 作为只读 Docker secret file 注入, when 加载 typed settings, then 目标字段取得文件内容且任何错误、日志或公开 evidence 不回显原值。

> `AC-008` 与 `AC-063` 的验收证据必须覆盖公共 loader、四类 application startup 入口、wheel-only 合同、真实 service smoke，以及异常链与 traceback frame locals 的脱敏回归；具体实现进度和变更生命周期不写入本产品契约。

### REQ-005: 存储、迁移与事务边界

**优先级：** P0  
**关联任务：** TASK-004, TASK-005, TASK-008  

**用途：**  
支持 checkpoint、session、run、approval、eval、trace、policy、audit 等核心状态。

**行为：**
- P0 使用 SQLAlchemy 2.0 typed declarative 纯写法。
- P0 使用 Alembic 管理迁移。
- P0 使用 Repository + Unit of Work。
- service profile 默认 PostgreSQL。
- local profile 默认 SQLite + filesystem。

**规则：**
- MUST `app/api`、`agents/*`、eval runner 不直接操作 SQLAlchemy session。
- MUST 所有核心表预留 `tenant_id`。
- MUST `agent_harness.storage` 暴露 repository 接口。
- MUST `UnitOfWork` 管理事务边界。
- MUST `agent-harness doctor` 检查数据库连接、迁移版本、Redis、provider key、OTel sink、eval 目录权限。
- MUST 核心存储层不使用 SQLModel。

**核心实体：**
- tenants / identity defaults
- agent_runs
- sessions
- checkpoints
- approvals
- policy_rules
- audit_logs
- eval_cases
- eval_runs
- trace_refs
- tool_invocations
- artifacts

**验收标准：**
- [x] AC-010: Given local profile, when 执行 migration, then SQLite schema 创建成功。
- [x] AC-011: Given service profile, when 执行 migration, then PostgreSQL schema 创建成功。
- [x] AC-012: Given repository contract tests, when 对 SQLite 和 PostgreSQL adapter 运行, then 行为一致。

### REQ-006: Durable runtime、checkpoint 和 resume

**优先级：** P0  
**关联任务：** TASK-001, TASK-006  
**关联流程：** FLOW-003

**用途：**  
支持长任务、审批等待、失败恢复和 durable execution。

**行为：**
- `agent_harness.runtime` 定义 `RunOrchestrator`、`CheckpointStore`、`ResumeToken`、`ApprovalWaitState`、`IdempotencyKey`。
- service profile P0 默认 DBOS adapter。
- local profile P0 使用 SQLite-backed checkpoint。
- P1 提供 Temporal adapter。

**规则：**
- MUST 业务 agent 不直接依赖 DBOS/Temporal。
- MUST approval wait state 可持久化。
- MUST resume 后事件 `seq` 继续递增，不重置。
- SHOULD 支持 idempotency key 防重复提交。

**验收标准：**
- [x] AC-013: Given run 触发 approval, when 进程重启后 approve, then run 可从 checkpoint resume。
- [x] AC-014: Given 同一 idempotency key 重复提交, when 创建 run, then 不产生重复 run。

> Checkpoint/resume 基础 seam、idempotency 与 `AC-013` 均已完成。contract tests 已证明 waiting checkpoint/approval 持久化后，使用同一 storage 重建 registry、executor resolver、orchestrator 和 approval service，再由 approval resolve 取得私有 lease、生成绑定 `ApprovalGrant` 并通过 runtime 内部 resume 恢复原 continuation；公开 resume token 仍不能代替该执行链。

### REQ-007: 多 agent registry 与受控 delegation

**优先级：** P0  
**关联任务：** TASK-002  
**关联流程：** FLOW-002

**用途：**  
脚手架可承载不同类型 agent，并为 multi-agent 留出正门。

**行为：**
- `AgentRegistry` 加载多个 `AgentDescriptor`。
- 每个 agent 有独立 `agent_id`、输入输出 schema、工具策略、模型策略、预算、eval dataset。
- HTTP 路由支持 `/api/v1/agents/{agent_id}/runs`。
- CLI 支持 `agent-harness run <agent_id>`。
- agent 可通过受控 tool 调用另一个 agent。
- 获准的真实 delegation 在 parent run 上发布固定的内部生命周期事件，供恢复、审计和后续 SSE 授权读取复用。

**规则：**
- MUST trace/eval/approval/storage 全部带 `agent_id`。
- MUST delegation edge 在 config/policy 中显式声明。
- MUST parent run 聚合 delegated run usage、trace 和 budget。
- MUST root run 创建时冻结 parent execution tree 的 token/cost hard limits 与 registry/config/catalog、root/target descriptor、route、price versions；root direct model/embedding、delegation top-level claim 和 child allocation 必须以 root `run_id` 为同一非空 `budget_owner_run_id`，原子竞争同一 durable ledger，不能把各 child 的 per-run budget 相加放大总额度。
- MUST child 只可进一步收紧 parent 已启用维度：token ceiling 取 parent hard limit 与 target ceiling 的更严者；parent cost 关闭时 child 不得重新启用 shared cost，parent cost 启用时 target null 不得取消 parent cost 上界。active、unknown 或 needs-review allocation 保守占用额度，不能按零或提前释放。
- MUST RUN-002 的 `delegation_summary.children` 以持久化 parent-child relation 为成员真相源，而不是以 terminal aggregate row 是否已生成来判断。child 已持久化后，即使仍为 `created|running|waiting`，或已终态但 aggregation 尚未结算，也必须返回其 `run_id`、`agent_id`、durable `RunStatus` 与 trace refs；这些未结算 child 的 token/cost/latency 保持 unknown，并强制 parent `budget_status=incomplete`。已结算与未结算 child 并存时必须全部返回，只聚合已知数值且整体仍为 incomplete；只有 parent 确实不存在带 `child_run_id` 的 durable relation 时 `delegation_summary` 才为 null。
- MUST 默认禁止任意 agent 互调。
- MUST 每次获准 delegation 最多发布三条业务事件，顺序固定为 `delegation.claimed` -> `delegation.child.created` -> `delegation.completed|delegation.failed`；final 两种类型互斥。child 创建前的确定性执行失败只发布 claimed 与 failed；edge/policy/tenant/cycle/depth/budget/idempotency/event-capacity 拒绝不发布 delegation 业务事件。
- MUST 四种 delegation 事件都归属 parent `run_id`，携带 parent canonical `trace_id` 与 source `agent_id`，并固定为 `record_scope=run`、`visibility=internal`、`terminal=false`。稳定 event id 分别为 `delegation:{delegation_id}:claimed`、`delegation:{delegation_id}:child`、`delegation:{delegation_id}:final`；重试或 worker reclaim 只能校验或补投同一稳定事件，不得增加生命周期事件数。
- MUST delegation payload 的公共字段只保留 `delegation_id`、`source_agent_id`、`target_agent_id`。claimed 只增加 `status=claimed`；child.created 增加 `child_run_id` 与封闭的 `status=queued|running|completed|failed`；completed/child 已创建后的 failed 增加严格符合 API Contract 5.30 `DelegationSummary` 的脱敏 `summary`，child identity 只由 `summary.children` 表达，final 不增加顶层 `child_run_id`；pre-child failed 不含 `child_run_id` 或 `summary`。除 failed 的稳定 `error_code=delegation.execution_failed` 外，不得包含 child input、完整 identity/request hash、动态余额、原始 usage、resume token、secret、本地路径或原始异常。未知结果保持 budget/event reservation 为 reserved/needs_review，阻止 parent terminal 且不发布 completed/failed final。

**验收标准：**
- [x] AC-015: Given 两个 agent, when 未声明 delegation edge, then agent A 调用 agent B 被 policy 拒绝。
- [x] AC-016: Given 已声明 delegation edge, when agent A 委派 agent B, then usage/budget/trace 归并到 parent run，且 RUN-002 对仅活动 child、已终态但尚未结算 child、已结算与未结算 child 并存三类 durable relation 都不遗漏；未知维度保持 null/incomplete，只有确无 child relation 时 summary 为 null。

### REQ-008: API、CLI 与管理面

**优先级：** P0  
**关联任务：** TASK-001, TASK-006, TASK-007  

**用途：**  
提供后端服务入口和本地管理入口，不做完整前端产品。

**行为：**
- API MUST 使用 `/api/v1/...`。
- API schema MUST 使用 Pydantic。
- CLI MUST 使用 Typer。
- P0 不做 React/Vue 管理台。
- P0 使用 OpenAPI/Swagger/Redoc 作为 API 管理面。

**P0 API：**

```text
/api/v1/agents
/api/v1/agents/{agent_id}/runs
/api/v1/runs/{run_id}
/api/v1/runs/{run_id}/events
/api/v1/runs/{run_id}/events/stream
/api/v1/runs/{run_id}/cancel
/api/v1/runs/{run_id}/resume
/api/v1/runs/{run_id}/approvals
/api/v1/runs/{run_id}/approvals/{approval_id}
/api/v1/eval-cases/drafts
/api/v1/eval-cases/approved
/api/v1/evals/runs
/api/v1/policies/check
/api/v1/health
```

**P0 CLI：**

```text
agent-harness doctor
agent-harness agents list
agent-harness run <agent_id>
agent-harness approvals list
agent-harness approvals approve <approval_id>
agent-harness approvals deny <approval_id>
agent-harness eval drafts
agent-harness eval approve <draft_id>
agent-harness policy check ...
agent-harness scaffold agent <agent_id>
```

**规则：**
- MUST API response 带 `request_id`，run 相关 response 带 `run_id`。
- MUST 内部错误统一转 API error envelope。
- MUST SSE 事件类型固定枚举。
- MUST 破坏性 API 变更进入 `/api/v2`。

**验收标准：**
- [x] AC-017: Given OpenAPI schema, when 运行 schema 测试, then P0 endpoints 均存在。
- [x] AC-018: Given CLI, when 执行 `agent-harness doctor`, then 输出 profile、storage、queue、observability、eval 目录状态。

### REQ-009: 租户、身份与认证

**优先级：** P0  
**关联任务：** TASK-001, TASK-003  

**用途：**  
减少未来多租户重构成本，保证所有核心实体和 trace/eval 都带身份上下文。

**行为：**
- P0 永远有租户上下文。
- 单用户/未启用多租户时使用默认租户。
- P0 使用 API Key / Bearer Token。
- P1 增加 OIDC/OAuth2 adapter。

**IdentityContext：**

```text
tenant_id: str = "default"
user_id: str = "local-user"
session_id: str
roles: list[str]
permissions: list[str]
auth_method: str
```

**规则：**
- MUST 所有核心实体带 `tenant_id`。
- MUST 权限、路径边界、预算、观测标签都按 `tenant_id` 预留。
- MUST 未启用多租户时认证层注入默认 tenant/user。
- MUST 所有权限判断只看 `IdentityContext` / `PermissionContext`，不直接耦合认证实现。

**验收标准：**
- [x] AC-019: Given 未配置多租户, when 创建 run, then run/session/trace/eval 均带 `tenant_id="default"`。
- [x] AC-020: Given 无效 Bearer Token, when 调用 P0 API, then 返回认证错误且不创建 run。

### REQ-010: PolicyEngine、权限拦截与 HITL

**优先级：** P0  
**关联任务：** TASK-003, TASK-006  
**关联流程：** FLOW-003

**用途：**  
从第一版放入权限内核，避免后续补权限时穿透全系统。

**行为：**
- `PermissionContext`：tenant/user/roles/permissions/session/agent。
- `PolicyDecision`：`allow` / `deny` / `require_approval`。
- `PolicyEngine` 输入 actor、resource、action、context，输出 decision。
- `InputGuardrail` 在用户/API/CLI 输入进入 run 前执行轻量过滤、注入风险检测和 trust marker 标注。
- 默认策略 provider 支持 YAML 和数据库 provider。
- CLI/HTTP 审批入口支持 approve/deny。

**拦截点：**
- 用户/API/CLI 输入进入 agent run 前
- agent run 启动前
- tool 调用前
- tool/MCP/retrieval output 回填上下文前
- 文件读写前
- shell/命令执行前
- MCP connector 调用前
- 模型 provider 调用前
- trace/eval/artifact 读取前
- 危险动作执行前转 HITL

**默认 require_approval 清单：**
- 删除文件或批量改文件
- 执行 shell 命令
- 访问非工作区路径
- 调用外部网络或 MCP 外部连接
- 发送邮件/消息/工单等对外动作
- 单次模型调用预计超过预算阈值
- 写入 approved eval dataset
- 修改权限策略本身

**规则：**
- MUST 默认清单可通过 YAML 或 DB provider 覆盖。
- MUST 审批记录写 audit log。
- MUST 审批状态和 checkpoint/resume 关联。
- MUST 默认开发策略允许 default 租户常规操作，但危险动作仍可配置审批。
- MUST guardrail 检查结果写入 trace/audit；阻断时不得创建不可恢复的半截 run。

**验收标准：**
- [x] AC-021: Given shell tool 默认策略, when agent 请求执行 shell, then 返回 `approval.required`。
- [x] AC-022: Given 审批通过, when resume run, then 原 tool call 继续执行且 audit log 记录审批人和结果。
- [x] AC-023: Given 策略为 deny, when 执行动作, then 动作不执行且 audit log 记录拒绝。
- [x] AC-024: Given 输入包含明显 prompt injection 或越权指令, when 创建 run, then guardrail 记录检查结果并按策略 allow / deny / require_approval。

### REQ-011: 工具系统、Shell、File 和 MCP

**优先级：** P0  
**关联任务：** TASK-003  

**用途：**  
统一工具注册、校验、权限、trace 和审计。

**行为：**
- `ToolRegistry` 管理本地工具、MCP 工具和内置工具。
- `ShellTool` 内置但默认 disabled。
- `FileTool` 只访问 workspace 根目录。
- MCP client 支持 stdio 和 HTTP/SSE。

**FileTool P0：**
- read_file
- write_file
- list_files
- search_files
- apply_patch
- delete_file

**ShellTool P0：**
- 显式启用
- workspace sandbox
- command allowlist/denylist
- timeout
- stdout/stderr 截断
- 环境变量白名单
- 结构化结果：exit_code、stdout_ref、stderr_ref、duration_ms、truncated

**MCP P0：**
- 配置 MCP server 列表
- tool discovery
- MCP tool allowlist
- 调用前 policy
- 调用结果写 trace/audit
- MCP 返回内容标记为 untrusted，进入 Context Assembly 前执行截断、来源标注和注入检测

**规则：**
- MUST 工具入参出参 schema 校验。
- MUST 大 tool output 走 artifact/ref，不直接塞进事件正文。
- MUST delete、批量写、workspace 外访问默认 require_approval。
- MUST 支持 `.agentignore`。
- MUST tool/MCP output 不得直接拼进 prompt；必须通过 Context Assembly 带 source、artifact_ref、trust_level 和 token budget。

**验收标准：**
- [x] AC-025: Given workspace 外路径, when FileTool read, then 默认拒绝或要求审批。
- [x] AC-026: Given MCP tool 未在 allowlist, when agent 调用, then policy 拒绝。
- [x] AC-027: Given shell 输出超过上限, when tool 完成, then stdout/stderr 被截断且 artifact_ref 可用。
- [x] AC-028: Given MCP tool output 包含指令型文本, when 写入上下文, then 系统保留来源和 untrusted 标记，并经过注入检测或截断。

### REQ-012: 模型、预算、上下文组装与 embedding

**优先级：** P0  
**关联任务：** TASK-003, TASK-004  

**用途：**  
统一模型路由、fallback、成本、超时、上下文组装和 embedding 调用。

**行为：**
- `ModelRouter` 支持 deployment-aware 的默认模型、任务级模型、fallback、timeout、重试、并发舱壁、能力与成本预算；route identity 与 provider SDK 对象分离。
- `ContextAssembler` 统一收口 system/user/history/retrieval/tool output/artifact refs，执行历史裁剪、检索注入、结果截断和 trust marker 传播。
- `EmbeddingProvider` 支持 OpenAI-compatible embedding API。
- P0 提供 local/mock embedding，用于测试和 CI。
- P0 提供 embedding cache。
- P0 的 local/fake model 永远可离线运行；真实文本模型由 REQ-025 的受控 deployment 配置显式启用，不能靠宿主机存在某个 provider 原生环境变量而自动开启。

**规则：**
- MUST 业务 agent 不直接写 provider 细节。
- MUST 请求中的 provider/model 选择只表达受限意图；最终 route 必须是部署允许列表、Agent descriptor 冻结策略和请求意图的交集，任一较低信任层都不能扩大上一层授权。
- MUST 在预算预约、授权成功审计和 provider 网络副作用前生成 immutable route plan，固定 deployment、provider、model、endpoint origin、capability、timeout/retry、price identity 与 credential reference；失败路径不得部分调用 provider，但可以提交不含秘密且不声称已授权的本地拒绝审计。
- MUST endpoint 与 credential 绑定；只有通过 scheme/origin allowlist 的 endpoint 才能取得对应 credential，禁止把 secret 转发到请求、Agent YAML 或未经批准的 `base_url`。
- MUST 模型/embedding 调用记录 token、cost、latency trace。
- MUST token、cost、latency 证据拒绝 bool、负数与非有限值；cache hit 仍记录本次调用级 evidence，但不得把首次 provider latency、token 或 cost 伪装成本次 provider 调用。
- MUST 单次模型调用预计超过预算阈值时触发 policy。
- MUST 软 review threshold 与 execution-tree shared hard limit 分层：先形成受信有限 intent 并通过静态 hard eligibility，再进入 soft policy/fallback/approval，最后在外部副作用前以 owner ledger 当前余额原子 reservation；审批不能提高、重置或覆盖 hard limit。
- MUST model、embedding miss、embedding cache hit、delegation 与 child allocation 使用版本化 immutable identity；stable operation key 只定位记录，tenant-scoped keyed request fingerprint 与完整 identity 决定 exact replay 或 conflict。Fingerprint secret 只能来自 REQ-004 typed settings，原值不得进入 snapshot、event、trace、audit、error 或持久化字段。
- MUST direct 与 delegation 共用同一 parent execution-tree ledger；`max_tokens_per_run` / `max_cost_usd_per_run` 在 P0 预发布阶段表示该 tree 的 shared hard limit，公开字段 shape 不变。`max_cost_usd_per_run=null` 只关闭 shared cost 维度，token 维度仍必须预约、结算、恢复和 terminal fencing。
- MUST usage application UoW 按固定优先级处理：exact replay/identity conflict → authorization/owner/relation/snapshot → `event.sequence_state_invalid` → budget → `event.sequence_exhausted` → unique-race reread；前序失败不得被后序 budget/capacity 错误覆盖。
- MUST 所有注入模型上下文的外部内容保留 `source_ref`、`trust_level` 和截断信息。
- MUST ContextAssembler 在超预算时按可解释顺序降级：裁剪历史、压缩记忆、截断 tool/retrieval output、切换 fallback model 或触发 policy。
- SHOULD 支持 cheap/flagship/local model routing。

**验收标准：**
- [x] AC-029: Given fake model provider, when 运行 tests/eval, then 不需要真实 API key。
- [x] AC-030: Given 预算阈值, when 模型调用预计超阈值, then 产生 policy decision 或可追踪 fallback。
- [x] AC-031: Given 重复 embedding 输入, when 第二次调用, then 命中 cache 或记录 cache miss 原因。
- [x] AC-032: Given 历史、检索和 tool output 同时进入上下文, when 组装 prompt, then 输出 context assembly trace，包含来源、可信级别、token 预算和截断记录。
- [x] AC-064: Given model、embedding provider 或 embedding cache 完成一次调用, when 记录 provider-neutral evidence, then 非负且有限的 token、cost、latency、provider/model、cache/provider side-effect decision 和 budget decision 可由同一 run/trace 关联，且业务 agent 不拼接 provider 原始事件。
- [x] AC-068: Given 一个 root execution tree 同时发生 direct model/embedding、delegation 与 child allocation, when 多进程并发预约、结算、崩溃恢复或 terminal, then 所有 operation 以同一非空 root owner ledger 竞争冻结的 token/cost hard limits，exact replay 不重复扣减，conflict/unknown/needs-review fail closed，拒绝路径无 provider/child/queue 副作用，且 SQLite 与真实 PostgreSQL 逐值一致。

### REQ-013: Retrieval 与 RAG

**优先级：** P0  
**关联任务：** TASK-002, TASK-004  

**用途：**  
提供 RAG 样例和检索扩展点，但不把系统做成 RAG 平台。

**行为：**
- `RetrievalProvider` 抽象。
- BM25 lexical retrieval 是 P0 必备能力。
- local profile 提供轻量 BM25。
- service profile 提供 PostgreSQL 检索 adapter。
- PGroonga adapter 是 P0 optional adapter，面向 CJK/multilingual full-text search。
- pgvector adapter 是 P0 optional adapter。
- hybrid retrieval + RRF 是 P0 interface。

**规则：**
- MUST PGroonga 未安装时可降级，不让系统起不来。
- MUST `agent-harness doctor` 检测 PGroonga/pgvector extension 状态。
- MUST RAG assistant 示例验证引用、检索 eval、trace。
- MUST 检索 chunk 一律作为 untrusted input 处理；注入上下文前必须带 citation/source_ref/trust_level，并经过 Context Assembly 的截断和注入检测。
- SHOULD OpenSearch/Elasticsearch/Vespa 放 P1/P2。

**验收标准：**
- [x] AC-033: Given local profile, when RAG 示例检索, then 不依赖 PostgreSQL 扩展也能返回结果。
- [x] AC-034: Given service profile 且 PGroonga 未安装, when doctor, then 输出降级提示而不是启动崩溃。
- [x] AC-035: Given hybrid retrieval adapter, when 提供 BM25/vector 结果, then 可执行 RRF 合并。
- [x] AC-036: Given 检索结果包含 prompt injection 文本, when 注入上下文, then 作为 untrusted citation 内容处理，不得覆盖 system / policy / developer 指令。

### REQ-014: CanonicalEvent 与流式输出

**优先级：** P0  
**关联任务：** TASK-001, TASK-006, TASK-007  

**用途：**  
统一内部事件模型，避免直接暴露 Pydantic AI、Claude、Logfire、Langfuse 等原始事件。

**行为：**
- P0 定义 `CanonicalEvent`。
- P0 提供 SSE adapter、CLI stream adapter、local/jsonl sink、OTel mapping。
- P1 提供 AG-UI / Vercel AI adapter。

**CanonicalEvent envelope：**

```text
event_id
event_type
event_version
seq
timestamp
tenant_id
user_id
agent_id
run_id
parent_run_id
trace_id
span_id
visibility
payload
payload_ref
raw_event_ref
```

**P0 事件类型：**

```text
run.queued
run.started
run.resumed
run.completed
run.failed
run.cancelled
model.request.started
model.output.delta
model.output.completed
model.structured.delta
model.structured.completed
model.usage.updated
input.guardrail.checked
input.guardrail.blocked
reasoning.delta
tool.call.args_delta
tool.call.started
tool.call.completed
tool.call.failed
retrieval.query.started
retrieval.query.completed
context.assembly.started
context.assembly.completed
policy.decision
approval.required
approval.resolved
delegation.claimed
delegation.child.created
delegation.completed
delegation.failed
checkpoint.created
context.compaction.started
context.compaction.completed
eval.case.drafted
eval.case.approved
eval.run.started
eval.run.completed
eval.score.recorded
artifact.created
```

该目录定义 `CanonicalEvent` 可以承载的稳定事件词汇，不表示每种事件已经有生产者。当前 AC-038 / RUN-006 已完成的是对已持久化事件的授权读取、SSE 映射和按 seq 恢复；真实模型 `model.output.delta` / `model.output.completed` 的生产、分片、容量、顺序、取消和部分 usage 由 REQ-026 / Phase 18.1 单独验收。

**规则：**
- MUST 每个 run 内 `seq` 单调递增。
- MUST `terminal=true` 当且仅当 event type 为 `run.completed` / `run.failed` / `run.cancelled`；三种 run terminal event 必须设置 `terminal=true`、`visibility=public`，其他类型必须设置 `terminal=false`。EventBus 与所有持久化 sink 必须在分配 seq、消费容量、物化 artifact 或 fan-out 前双向拒绝 type/terminal/visibility 不一致的 envelope。每个 run 只能有一个 terminal event；它是该 run 的最后一条 CanonicalEvent，持久化后必须拒绝任何后续业务事件。
- MUST usage 结算、approval resolution 等 terminal 前置 evidence 由 durable outbox/settlement 状态协调；terminal 一旦可见，所有必需前置 evidence 必须已经存在，恢复只能重放稳定 event id，不能重放 provider/tool 副作用。
- MUST parent terminal guard 同时校验 shared-budget ledger：存在未封闭 direct claim、delegation top-level claim、child allocation、`side_effect_state=started` 且无可信结果、unknown 或 needs-review 时不得发布 terminal；恢复只能补齐既有 claim/settlement/outbox，不能重放 provider、child 或 queue 副作用。
- MUST delegation 生命周期只使用本节列出的四种 internal non-terminal event，遵守 REQ-007 的最多三条顺序、稳定 event id、阶段 payload 和 needs_review 无 final 规则；RUN-003、CLI 与后续 RUN-006 默认隐藏 internal event，只有通过 tenant/run 授权并显式请求 internal visibility 的 reader 才能读取 canonical event，不生成公开别名或第二套事件。
- MUST run 创建时预留一个 terminal event 容量；任何可能产生后续 evidence 的 provider/tool/approval/delegation 副作用开始前，durable outbox 必须按受信、版本化、封闭的 operation kind 计算最大 event 数并原子预留，调用方不能自报较小数值。当前已持久化的最高 `seq`、未结算预约和 terminal 预约之和不得超过 CanonicalEvent `seq` 上限；不能用 event row count 代替最高 `seq`，因为历史或直接写入可能留下空洞。容量不足时必须在外部副作用前拒绝，不能等到结算阶段才发现无 seq 可写。
- MUST `tool.call.args_delta` 可表示半截 JSON，不要求每个 delta 可解析。
- MUST `reasoning.delta` 默认不对普通用户暴露。
- MUST hook/policy/approval 控制事件进 audit/local-jsonl，即使不推给前端。
- MUST 大 payload 走 `payload_ref`。
- MUST 正常写入的 CanonicalEvent envelope 使用全局唯一的 canonical JSON serializer 计数并且不超过 `65536` bytes；serializer 对 `CanonicalEvent.to_payload()` 使用 UTF-8、`ensure_ascii=false`、排序键、紧凑分隔符并拒绝 NaN，换行和 SSE frame 前缀不计入 envelope bytes。payload 超限先 artifact 化，artifact 化后 envelope 仍超限则在持久化前稳定拒绝。EventBus、local JSONL、PostgreSQL/SQLite 校验和 SSE byte page 必须复用该 serializer；历史或直接数据库写入的超限 row 只能 fail closed，不能让 SSE reader 返回空页忙循环。
- MUST SSE 是输出协议，不是内部事件模型。
- MUST SSE 断线恢复以 `CanonicalEvent.seq` 为准；HTTP SSE adapter 应把 `Last-Event-ID` 映射为续读起点。
- MUST 任何新增流式 producer 在外部副作用前为有界最大事件数预约容量；事件类型已经存在不能绕过 operation-kind registry、durable outbox、terminal reservation 或 envelope 上限。

**验收标准：**
- [x] AC-037: Given 一个 run, when event stream 完成, then terminal event 只有一个。
- [x] AC-038: Given SSE 客户端断开后按 seq 恢复, when 重新连接, then 可继续获取未读事件。
- [x] AC-039: Given 普通用户 visibility, when reasoning event 产生, then 默认不发送给用户流。
- [x] AC-040: Given guardrail/context assembly 事件产生, when 写入 local/jsonl, then 包含 source/trust/truncation 摘要但不泄露 secret 或完整大 payload。
- [x] AC-067: Given CanonicalEvent catalog 与真实 delegation 生命周期, when 事件由 EventBus 或 local/PostgreSQL sink 接受并发生重试、失败或 needs_review, then 39 种固定类型与代码枚举精确一致，terminal type/flag/visibility 双向一致，delegation 最多三条且顺序、稳定 event id、payload、internal 可见性和零拒绝副作用均满足本规格。

### REQ-015: Observability 转换层

**优先级：** P0  
**关联任务：** TASK-007, TASK-008  
**关联流程：** FLOW-004

**用途：**  
OTel 是底座协议，不是业务边界；Logfire/Phoenix/Langfuse 必须走转换层。

**行为：**
- `agent_harness.observability` 提供 `TelemetryFacade`。
- runtime events 转 OTel Span/Metric/Event。
- provider adapter 支持 local/jsonl、Logfire、Phoenix、Langfuse。
- P0 默认 local/jsonl + OTel，文档推荐 Logfire。

**规则：**
- MUST 业务 agent 不直接 import Logfire/Phoenix/Langfuse SDK。
- MUST local/jsonl 永远可用，不能因为配置外部 provider 就删除。
- MUST trace/span 关联 tenant、user、agent、run、session、tool、model、eval。
- MUST secret 默认脱敏。
- SHOULD 外部 provider 失败时本地证据不丢。

**验收标准：**
- [x] AC-041: Given 未配置任何 SaaS provider, when 运行 agent, then local/jsonl 仍产出 trace。
- [x] AC-042: Given 配置 Logfire adapter, when 运行 agent, then provider adapter contract test 通过且业务代码无 Logfire import。

### REQ-016: Eval Gate 与 trace/eval 闭环

**优先级：** P0  
**关联任务：** TASK-007, TASK-008  
**关联流程：** FLOW-004

**用途：**  
让 eval 和 observability 形成工程闭环，而不是并排摆设。

**行为：**
- `agent_harness.evals` 提供 `EvalCaseFactory`、`EvalRunner`、`ScoreSink`、`ReviewDatasetAdapter`。
- P0 支持从低分 trace / failed run 半自动生成 eval case。
- P0 Trace 转 Eval Case 必须人工确认后入库。
- P0 支持把 eval score 写回本地 JSONL 和可配置观测 provider。
- P0 示例 agent 完成后，eval 数据集必须支持 behavior tags、optimization / holdout split、baseline run、candidate harness run、regression report 和人工 acceptance gate。
- P1 支持满足规则后自动入库，默认关闭。

**流程：**

```text
Runtime Trace
  -> TraceCollector
  -> Failure / Low-score Detector
  -> EvalCaseDraft
  -> Human Review Queue
  -> Eval Dataset
  -> Tagged Optimization / Holdout Sets
  -> Baseline Experiment
  -> Candidate Harness Experiment
  -> Regression / Holdout Review
  -> Human Acceptance Gate
  -> Eval Runner
  -> ScoreSink
  -> Observability Provider
```

**规则：**
- MUST `eval-cases/drafts` 和 `eval-cases/approved` 分离。
- MUST `make eval` 只跑 approved cases。
- MUST approved dataset 写入默认 require_approval。
- MUST draft 到 approved 的过程检查 secret/隐私脱敏。
- MUST eval case 支持行为标签；标签至少能覆盖 tool selection、retrieval quality、follow-up quality、policy/approval、context/trust boundary 这类可独立优化的行为类别。
- MUST optimization / holdout 拆分按标签可追踪；优化只能看 optimization set，holdout 用于验收候选 harness 的泛化。
- MUST baseline 和 candidate harness 运行都记录 harness version、agent id、dataset split、score summary、regression summary、local/provider evidence ref。
- MUST harness 变更接受需要人工 review；系统不得自动把候选 harness 写成 accepted production harness。
- SHOULD P1 接入 Logfire Hosted Datasets、Phoenix dataset/eval workflow、Langfuse annotation/dataset/score。

**验收标准：**
- [x] AC-043: Given failed run trace, when 执行 `agent-harness eval draft`, then 生成 draft case。
- [x] AC-044: Given draft case, when 人工 approve, then case 进入 approved dataset 并写 audit log。
- [x] AC-045: Given approved dataset, when `make eval`, then 产出 eval result 和 score sink 记录。
- [x] AC-045A: Given approved cases with behavior tags, when 创建 experiment split, then optimization / holdout sets 按标签可追踪且不会把 draft case 纳入评分。
- [x] AC-045B: Given baseline harness 和 candidate harness, when 执行 experiment compare, then 输出 per-tag score delta、holdout result、regression summary 和人工 acceptance 所需证据。

### REQ-017: 示例 agent

**优先级：** P0  
**关联任务：** TASK-002, TASK-004  

**用途：**  
用薄样例验证脚手架扩展点，不把样例做成完整产品。

**P0 示例：**
- RAG assistant：验证 retrieval、引用、RAG eval。
- ticket triage：验证结构化输出、分类 eval。
- repo analyst：验证文件读取、workspace 边界、长上下文。
- dev assistant：验证 shell/file tool、HITL、危险动作、命令审批。

**规则：**
- MUST 每个示例有 agent config、工具策略、eval cases、测试。
- MUST 示例不直接 import 厂商 SDK。
- MUST 示例覆盖不同能力块，避免四个样例都只是 prompt demo。

**验收标准：**
- [x] AC-046: Given P0 示例 agent, when `agent-harness agents list`, then 四个示例均可见。
- [x] AC-047: Given 每个示例和全新的评测 `STATE_DIR`, when 执行对应 `make eval*`, then 入口先显式迁移其独立 SQLite，再在 fake model 下跑通确定性评测。

### REQ-018: README 与文档体系

**优先级：** P0  
**关联任务：** TASK-001, TASK-008, TASK-011  

**用途：**  
README 是入口，深度文档解释架构和维护边界。

**README MUST 包含：**
- 根目录与 `templates/service-app` 都维护英文主入口 `README.md` 和中文入口 `README.zh-CN.md`；同目录两份文档互相链接，并保持命令、能力边界和已实现状态一致。
- 同时服务英文和中文入口的新增实操文档也采用“英文主文件 + `.zh-CN.md` 中文文件”配对，互相提供语言切换入口，并保持步骤、命令、边界和已实现状态一致。
- 项目定位、当前能力与明确的非目标，避免把本地验证、未来物理服务或 hosted-unverified 能力写成已部署事实。
- 准备环境、第一次使用、日常使用指南和常见故障排查；首次路径必须给出可复制命令，并说明 fingerprint key、migration、local/service profile 与外部依赖前置。
- CLI、HTTP/OpenAPI 与 Python 公共 API 的使用入口；如果提供 Make target、scaffold、profile/config、factory/helper 等便捷封装，必须说明它简化了什么、底层仍复用哪个公共 seam，并给出实际用法，不能只写“有语法糖”。
- Project Structure、主要模块设计思路、For Agent App Developers、For Scaffold Maintainers、开发与测试指南、贡献指南、Deep Docs、License & Compliance、Release Process；五层两翼架构必须提供“如何开发一个 Agent”的实际映射，区分框架已提供能力、业务开发者必改部分、按需扩展部分和未来目标位。
- README 只承载可靠上手路径与模块地图；字段级 API、复杂安全/恢复语义和 provider 合同链接到 `API-Contract.md` 与深度文档，避免复制后漂移。

**深度文档 MUST 包含：**
- `docs/architecture/README.md`
- `docs/extension-guide.md`
- `docs/adapter-contracts.md`
- `docs/context-and-trust-boundary.md`
- `docs/eval-observability-loop.md`
- `docs/security-policy.md`
- `docs/release-process.md`
- `docs/adr/`
- `docs/building-an-agent.md` 与 `docs/building-an-agent.zh-CN.md`，说明如何把五层两翼落实为可运行 Agent。
- 上述被英文 README、英文 building guide 或英文模板入口直接引用的维护文档，均以原路径作为英文主文件，并在同目录维护 `.zh-CN.md` 中文版；两版互链且命令、合同、当前/未来边界和证据状态一致。该规则同时覆盖 `docs/architecture/README*`、3 份 ADR、6 份深度专题文档，以及模板内 `docs/README*` 和 `docs/examples*`。

**AI / Agent 协作说明 MUST 包含：**
- `templates/service-app/docs/ai-agent-guide.md` 作为复制模板后可通过链接或明确提示交给 AI / Agent 的英文主说明，并维护内容等价的 `templates/service-app/docs/ai-agent-guide.zh-CN.md`；两份文件必须互链，不得使用 `AGENTS.md` 等会自动施加目录级指令的特殊文件名。
- 区分“框架源码仓库”和“已复制的 service-app”两种上下文：复制后的应用不得依赖只存在于源码仓库的 `Product-Spec.md`、`DEV-PLAN.md`、OpenSpec 或维护脚本；这些文件存在时可作为上游真相源，不存在时必须以模板内 README、配置、代码和测试为准。
- 给出 AI / Agent 可执行的项目初始化流程：确认目标目录与现有改动、选择受信任的 `agent-harness` 安装源、执行 bootstrap、准备本地环境与 fingerprint key、迁移、local smoke 和首次运行；默认不得使用生产凭据或执行部署。
- 给出功能实现流程：先把需求映射到五层两翼，按公共 seam 修改 Agent、工具、基础设施、Eval 与 Observability，并保持 `tenant_id`、`agent_id`、`run_id` 关联和业务 Agent 不直连厂商 SDK 的边界。
- 按改动类型选择最小充分验证，文档改动不得默认触发无关的全量测试；提交、push、部署、真实 provider 或 registry 副作用必须由用户单独授权。
- README 必须提供指南入口和一段可直接复制给 AI / Agent 的引导提示；指南提供可复制的“初始化项目”和“实现功能”任务模板，并要求交付时报告实际改动、已运行验证、结果与未验证边界，不能用“应该可用”代替证据。

**目录边界 MUST 写入 README：**
- `agents/*` 不直接 import 厂商 SDK，只走 `agent_harness`。
- `app/*` 不写业务 agent 逻辑，只负责协议入口和响应转换。
- `agent_harness/*` 不依赖具体示例 agent。
- `adapters/*` 可以依赖外部厂商包，但不能反向污染核心接口。
- `eval-cases/approved` 只能由审核流程写入。
- 所有运行记录必须带 `tenant_id`、`agent_id`、`run_id`。
- 多 agent delegation 必须走 registry 和 policy。

**验收标准：**
- [x] AC-048: Given 根目录、`templates/service-app` 的中英文 README、双语五层两翼指南和双语 AI / Agent 协作说明, when 新开发者或受托 AI / Agent 选择任一语言从零开始, then 能完成环境准备、项目初始化、首次运行、CLI/HTTP/Python API 定位和一个功能实现，并能识别目录职责、框架已提供能力、业务 Agent 必改部分、按需扩展部分、两翼使用方法、便捷封装、验证范围、授权边界与禁止跨边界规则。
- [x] AC-049: Given scaffold maintainer, when 从中英文任一入口阅读 docs, then 能以同一语言找到 architecture、extension、adapter/context、安全、eval/observability、release、ADR 和模板 examples，并且两种语言描述相同的命令、合同与验证边界。

### REQ-019: TDD、测试与质量门禁

**优先级：** P0  
**关联任务：** TASK-009  

**用途：**  
脚手架是基础设施，必须全面测试并按 TDD 开发。

**行为：**
- 每个能力块先写失败测试 / contract test，再实现。
- 禁止“先堆代码后补测试”作为 Phase 完成方式。
- 模板自带测试结构，让使用者天然按 TDD 开发 agent。

**测试结构：**

```text
tests/
├── unit/
├── contracts/
├── integration/
├── evals/
├── fixtures/
└── smoke/
```

**P0 fake/test doubles：**
- FakeModelProvider
- FakeEmbeddingProvider
- FakeTelemetrySink
- FakeMCPServer
- FakePolicyProvider
- FakeClock
- FakeStorage 或 SQLite test adapter

**工具：**
- ruff lint + format
- pyright typecheck
- pytest + pytest-asyncio
- coverage.py
- pre-commit

**命令：**

```text
make test
make integration
make eval
make smoke-local
make smoke-service
make quality
```

**验收标准：**
- [x] AC-050: Given 需求验收矩阵显式选择的能力块, when 审查当前基线, then 每个所选 REQ 及其全部 AC 可追踪到仓库内存在的具体生产文件、至少一个以 `path.py::test_name` 或 `path.py::TestClass::test_name` 表示且真实验证该行为的精确 pytest node、实际执行该验收行为的一个或多个 CI job，以及各 job 产生的 unit/contract/integration/eval/smoke evidence；validator 不得根据开发阶段或优先级标签推断范围，必须拒绝未列出父 REQ 的孤立 AC，并核验精确 node 存在且拒绝仅含 `pass` 或 `assert True` 的空壳。复合 AC 必须列出全部 producer，且 evidence command 必须执行对应 allowlisted Make target，其中 AC-001 的真实 `uv sync --frozen` 行为由 `install` producer 证明，AC-002 的真实 `uv build` 行为由 `build` producer 证明，AC-003/006 的 workspace 外 wheel 安装与复制模板运行由 `integration` producer 和对应集成测试证明，AC-004/061 必须执行示例/业务 Agent 的 vendor 与 ORM import 扫描，AC-005 必须实际构造 fake model adapter，AC-019 必须贯穿 run/session/CanonicalEvent trace/eval 的默认 tenant，AC-023 必须同时证明 deny 零动作副作用与拒绝 audit，AC-026 必须调用未入 allowlist 的 MCP tool 且不触网，AC-029/052 必须在无真实 provider key 时执行真实 fake-model eval，AC-062 必须分别覆盖 API、worker、tool/model adapter 与 CanonicalEvent 关联字段交换；AC-012/068 的 SQLite 行为由 `test-aggregate` 中的精确合同测试证明、PostgreSQL 行为由 `smoke-service` 证明，AC-011/060 的静态 smoke 合同与真实 PostgreSQL/service 行为分别由 `test-aggregate` 和 `smoke-service` 证明，AC-065 必须映射到从公开入口完成 single-agent fake run 并执行五秒门禁的正向测试，泛化目录、文件级测试映射、不存在 node、只提供 helper 的伪测试文件或 CI/evidence producer 错配均不得算映射；GitHub/GitLab 还必须各自用独立 required `acceptance-validate` 终端 job 下载或继承包括 `install`、`integration`、`build` 在内的全部 producer evidence 并阻断后续 promotion，不能依赖 `test-aggregate` 对已有证据的条件式自检；新 change 仍必须在实现前保留失败测试或未满足 contract 的 red 证据。
- [x] AC-051: Given CI quality job, when pipeline 执行, then `make quality` 与 `make test` 分别通过，且 ruff、pyright、import boundary、unit/contract tests 均有独立结果。
- [x] AC-052: Given `make eval`, when 未配置真实模型 key, then fake model eval 可通过。

### REQ-020: CI/CD 与 Release Automation

**优先级：** P0  
**关联任务：** TASK-009, TASK-010  
**关联流程：** FLOW-005

**用途：**  
从第一版保证核心包可构建、可发版、可追踪变更。

**行为：**
- P0 内置 `.github/workflows/ci.yml`。
- P0 内置 `.gitlab-ci.yml`。
- 两边跑同一套 make 命令。
- P0 release automation 支持 SemVer、tag、CHANGELOG generation、release notes、wheel/sdist artifact。
- P0 支持发布到私有 package registry；公开 PyPI 发布可后续决定。

**Release automation 推荐策略：**
- 使用 Conventional Commits 作为版本计算输入。
- Python 包构建使用 `uv build`。
- 发布使用 `uv publish` 或等价私有 registry job。
- GitHub 可使用 release-please 或 python-semantic-release。
- GitLab 可使用 python-semantic-release 或等价 semantic-release job。
- 无论底层工具不同，产物和门禁 MUST 一致。

**P0 CI jobs：**
- install with uv
- ruff
- pyright
- unit + contract tests
- local integration
- eval tests with fake model
- service smoke with PostgreSQL/Redis
- build wheel/sdist
- license check
- release dry-run 或 version check

**CI artifacts：**
- test report
- coverage report
- local/jsonl trace sample
- eval result
- smoke logs
- wheel/sdist
- generated CHANGELOG/release notes preview

**规则：**
- MUST release job 在质量门禁通过后运行。
- MUST 无 releasable commit 时不发版。
- MUST release process 写入 `docs/release-process.md`。
- MUST template 声明与当前发布版本精确匹配的 `agent-harness` 自依赖，例如 `==0.1.0`，避免模板与核心包跨版本组合。
- MUST 将每次发布预演记录为版本化、机器可读的 `ReleaseRecord` CI artifact；该记录不是运行时业务实体，不写入应用数据库。
- MUST 破坏性变更写 ADR。

**验收标准：**
- [ ] AC-053: Given GitHub CI, when push/PR, then `make quality`、`make eval`、`make smoke-local`、`make smoke-service` 执行。
- [ ] AC-054: Given GitLab CI, when pipeline, then 与 GitHub 等价命令通过。
- [x] AC-055: Given releasable commits, when release workflow dry-run, then 生成下一版本、CHANGELOG 预览、tag 名称和 wheel/sdist artifact。
- [x] AC-056: Given no releasable commits, when release workflow dry-run, then 不创建 tag 或 release。

### REQ-021: 开源合规与许可证

**优先级：** P0  
**关联任务：** TASK-011  

**用途：**  
从第一版满足开源协议规范，避免发布前补合规。

**行为：**
- 项目许可证为 Apache-2.0。
- 仓库根目录 MUST 有 `LICENSE`。
- 如引入需要声明的第三方代码或素材，MUST 维护 `NOTICE`。
- MUST 提供基础 `make license-check` 或等价脚本。

**规则：**
- MUST 复制第三方代码片段时记录来源、license、修改说明。
- MUST 不 vendoring 第三方源码，除非有明确 ADR 和 license 审查。
- MUST README 说明项目 license 和第三方依赖 license 审计方式。
- MUST 文档引用架构、外部库、官方能力时保留链接。

**验收标准：**
- [x] AC-057: Given 仓库根目录, when 检查 license 文件, then `LICENSE` 存在且为 Apache-2.0。
- [x] AC-058: Given 引入第三方片段, when review, then NOTICE/来源/license/修改说明可追踪。

### REQ-022: 部署边界与未来微服务拆分基础

**优先级：** P0  
**关联任务：** TASK-001, TASK-008, TASK-009  

**用途：**  
P0 先交付可运行脚手架，不强制微服务化；但必须从第一版定义可拆边界，避免后续把 API、runtime、工具执行、模型代理、存储和事件观测从进程内单体里硬撕出来。

**行为：**
- P0 MUST 支持 local profile 单进程开发运行。
- P0 MUST 支持 service profile 下 API 入口和 runtime worker 至少作为可分离进程运行。
- P0 MUST 在文档中定义未来可拆服务边界：Access/API gateway、Runtime worker、Model gateway、Tool gateway、Storage service、Event/Observability pipeline。
- P0 MUST 所有跨边界数据使用 Pydantic schema、CanonicalEvent、repository interface、provider interface 或明确 DTO，不允许直接传递 ORM session、SDK 原始对象或进程内可变全局对象。
- P0 MUST 所有跨边界调用带 `tenant_id`、`agent_id`、`run_id`、`request_id` 或 `trace_id` 中适用的关联字段。
- P0 MUST API/worker 通过 queue 协作时传递 `request_id` 和 `idempotency_key`，避免 Redis 重试或 worker pickup 超时导致重复 run。
- P0 MUST 工具执行、模型调用、存储访问、事件输出都通过 adapter/provider/facade，不允许业务 agent 直接依赖具体实现。
- P0 SHOULD 定义 `EventBus` / `EventSink` 抽象；默认实现可以是 local/jsonl、DBOS/Redis queue 或进程内测试 adapter，Kafka/RabbitMQ/NATS 放 P1/P2 adapter。
- P0 SHOULD 在 DEV-PLAN 中写明未来拆分顺序，默认先拆 worker，再拆 tool/model gateway，最后拆 observability/event pipeline。

**规则：**
- MUST P0 不把物理微服务、服务注册发现、WAF、Kubernetes、多 AZ 当作必交付功能。
- MUST README / `docs/architecture/README.md` 解释哪些模块今天同进程、哪些边界未来可拆、拆分时哪些接口不变。
- MUST CI 至少包含一条 service profile smoke，验证 API 进程和 worker 进程使用同一 storage/queue 配置时可协作。
- MUST 禁止为了图上好看提前引入分布式复杂度；边界优先，分布式实现后置。

**验收标准：**
- [x] AC-059: Given README / architecture docs, when 新维护者阅读部署边界章节, then 能指出 API、runtime worker、model/tool gateway、storage、event pipeline 的当前形态和未来拆分路径。
- [x] AC-060: Given service profile, when 分别启动 API 进程和 worker 进程并提交 run, then run 可被 worker 执行并通过共享 storage/queue 产出事件。
- [x] AC-061: Given 业务 agent 代码, when 静态扫描 import, then 不直接 import 具体 model/tool/storage/observability vendor SDK 或直接操作 ORM session。
- [x] AC-062: Given CanonicalEvent / DTO contract tests, when API、worker、tool/model adapter 交换数据, then 关联字段和 schema 校验保持一致。

### REQ-023: 依赖兼容范围与可复现解析

**优先级：** P0
**关联任务：** TASK-001, TASK-009, TASK-010

**用途：**
依赖声明表达项目支持的兼容窗口，`uv.lock` 表达当前经过验证的精确解析；不能把二者混成一组永久 exact pin，也不能以放宽声明为由让 CI、构建或发布自动漂移。

**行为：**
- 根 workspace、核心包、可选 extra、service-app 模板及其开发/构建工具中的外部依赖 MUST 使用带已验证下界和破坏性升级上界的 PEP 440 范围；稳定版本默认限制在下一主版本之前，`0.x` 依赖默认限制在下一次版本之前，已知上游组合约束可以更窄。根 workspace 与 service-app 模板对同仓库 `agent-harness` 的自依赖 MUST 精确匹配当前项目版本，避免模板和核心包跨版本组合。
- `uv.lock` MUST 继续记录完整精确解析；普通 `uv sync --locked`、`uv lock --check`、CI 和 release 不得仅因声明范围放宽而升级已锁包，升级必须通过显式 `uv lock --upgrade` 或等价受审动作发起。
- build-system metadata 可以向消费者声明兼容范围，但仓库 release preview 与正式 tag build MUST 先按 frozen lock 准备受审 build backend，再关闭默认 build isolation 使用该精确 backend；manifest MUST 记录并核对 backend identity，不能假定默认隔离构建会继承项目 lock。
- 根 `[tool.uv].required-version` 与 release wrapper MUST 接受 uv `>=0.11.29,<0.12`；GitHub 与 GitLab 当前执行环境继续具体选择 `0.11.29` 及受审 OCI digest，但该选择不得收窄 wrapper 的兼容范围。`release` preview、正式 build 与 publish plan MUST 记录各自实际使用的范围内 uv 版本，`no-release` preview MUST 记录 `uv_version: null` 且不得仅为填充证据启动 uv 或 build。
- 发布 promotion MUST 只更新本项目版本及两处 `agent-harness` 精确自依赖，不得放宽为兼容范围或通配形式。

**规则：**
- MUST exact pin 只出现在 lock、同仓库版本耦合的自依赖、CI image/action、发布 wrapper、容器 digest、合规快照或其他明确要求不可变身份的边界；若 `pyproject.toml` 的外部依赖确需 exact pin，必须就地写明兼容、安全或合规原因并有合同测试覆盖。
- MUST 声明范围变化与解析升级分开审查；只放宽且包含当前版本时，`uv.lock` 中的 `(name, version, source)` 身份不得变化。
- MUST 双语 README 与 release 文档明确区分“支持范围”“当前 lock 解析”“CI 具体工具版本”和“单次发布证据实际版本”。

**验收标准：**
- [x] AC-069: Given 三份 `pyproject.toml`, when 检查普通、optional、dev、license、release 与 build-system 声明, then 所有可放宽的外部依赖都含已验证下界和兼容上界；根 workspace 与 service-app 模板的 `agent-harness` 自依赖精确等于当前项目版本，且不存在其他无说明 exact pin。
- [x] AC-070: Given 当前 `uv.lock`, when 只将依赖声明改为包含当前版本的兼容范围并刷新 lock metadata, then 所有已锁 package 的 `(name, version, source)` 身份保持不变，`uv lock --check` 和 frozen sync 通过。
- [x] AC-071: Given release promotion 将版本从 `0.1.x` 提升到 `0.2.0`, when 更新根 workspace 与 service-app 模板依赖, then 两者都得到 `agent-harness==0.2.0`；uv `>=0.11.29,<0.12` 可读取当前 lock 并通过 release wrapper，CI 当前具体使用 `0.11.29`，单次发布证据记录实际 uv 版本。
- [x] AC-072: Given build-system metadata 使用 `hatchling>=1.30.1,<2`, when release preview 或正式 tag build 生成 wheel/sdist, then 构建入口只使用 frozen lock 中精确 `hatchling 1.30.1`，manifest 记录该 backend identity，任何缺失或漂移都在产物授权前 fail closed。

### REQ-024: 架构治理与持续演进纪律

**优先级：** P0
**关联任务：** TASK-008, TASK-009, TASK-011, TASK-012

**用途：**
把分层原则、设计原则、模式选择、代码规范和长期改造状态变成维护者与 Agent 共享的工程契约，避免靠单次对话记忆或一次性全仓重构维持质量。

**行为：**
- `docs/engineering-principles.md` 与中文版定义五层两翼的允许依赖方向、跨层 seam、设计原则和模式选择信号；`CONTRIBUTING.md` 与中文版定义人与 Agent 共用的代码、测试、验证、Git 和安全纪律。
- `docs/plans/architecture-evolution-plan.md` 是长期架构演进的 living plan，记录冻结基线、阶段 DAG、进度、发现、决策、风险、验证、恢复方式、下一动作与 handoff；`docs/plans/architecture-evolution-change-matrix.md` 记录 change 依赖、共享接口、验收和文件所有权。
- 架构演进按窄范围 change 进行：先固定问题和不变量，再定义可替换 seam 与失败合同，最后迁移调用方；已稳定的公共契约只在有明确收益和迁移计划时改变。
- 设计模式按变化轴和风险选择。现有 Adapter/Strategy、Repository/UoW、Facade、composition root、EventBus、object capability、transactional outbox 与幂等恢复继续作为已验证先例；State、Command、Decorator、Circuit Breaker、Bulkhead 等只在对应问题出现时引入。

**规则：**
- MUST 不以“使用了多少经典模式”验收设计，也不得为模式名称制造无变化轴的抽象；每个新增抽象必须说明保护的不变量、隔离的变化、替代方案和删除成本。
- MUST 单一职责、开闭、里氏替换、接口隔离和依赖倒置落实到模块所有权、public protocol/DTO 与合同测试；不能只写在文档里而允许实现反向依赖。
- MUST composition root 管理进程级对象生命周期；禁止用隐藏的全局可变 Singleton 传递配置、storage、provider、budget、event bus 或 request state。确需单实例生命周期时仍通过显式 factory/container 注入并可替换测试实现。
- MUST 跨层调用使用 DTO、protocol、facade、provider、repository/UoW 或 `CanonicalEvent`；vendor SDK、ORM session 和 provider 原始对象不得越过批准边界。
- MUST 可机械判定的依赖、vendor import、ORM、公开 seam 和配置规则进入 checker、contract test 或 CI；文档与图只解释意图，不得冒充执行证据。
- MUST 架构影响跨多个 session 时更新 living plan 的 `Progress`、`Surprises & Discoveries`、`Decision Log` 与 `Handoff Snapshot`；新 Agent 必须能从仓库文件和当前 Git/OpenSpec 状态续接，不能依赖上个对话的隐藏上下文。
- MUST 不做大爆炸重构。共享接口、共享验收或共享文件所有权的 change 串行接力；只有依赖、接口、验收和文件所有权均可证明独立时，才允许使用独立 worktree 并行。

**验收标准：**
- [x] AC-073: Given 人或 Agent 从中英文仓库入口开始维护, when 查找架构与代码规则, then 能进入事实等价的工程原则和贡献指南，并能指出允许依赖方向、模式选择信号、禁止边界与最小充分验证入口。
- [ ] AC-074: Given 一个受覆盖的反向层依赖、vendor SDK 泄漏、ORM session 越界或禁止配置路径, when 执行 `make quality` 或对应 contract, then 在实现合并前以稳定诊断失败；当前 checker 未覆盖的规则必须在 change 中显式列为剩余风险。
- [x] AC-075: Given 上下文被压缩或更换维护 Agent, when 只读取 Product Spec、DEV-PLAN、架构演进 living plan、change matrix、Git 与 OpenSpec 当前状态, then 能重建已决策事项、未决风险、文件所有权和唯一下一动作，不需要依赖历史对话。
- [ ] AC-076: Given 一项跨模块架构调整, when 进入实现, then 有窄 OpenSpec change、变化轴/不变量/备选方案说明、red contract、受影响文件所有权和回滚/恢复方式；不得以一次全仓重写替代分阶段验收。

### REQ-025: 受控真实文本模型运行时

**优先级：** P0
**关联任务：** TASK-003, TASK-004, TASK-008, TASK-013
**关联流程：** FLOW-006

**用途：**
把已经存在的 provider adapter 接入可控、可审计、可预算的真实文本模型路径，同时保留 local/fake 的离线确定性；真实模型不是“把 API key 放进某个文件然后让 SDK 自己发现”。

**行为：**
- `ModelSettings` 从单一 `provider/default_model/timeout` 扩展为 deployment-aware 配置。每个 deployment 至少声明稳定 id、provider kind、允许模型、默认/回退模型、`base_url` 或 provider 默认 endpoint、credential reference、连接/读取/总 deadline、有限 retry/backoff、最大并发/排队时限、价格目录引用与版本、能力标志。
- credential reference 指向同一 typed settings 树中的 secret 字段；秘密值可以在本地由被忽略的 `.env` 注入，也可以由进程环境或受控 `_FILE` 注入。profile 与 Agent YAML 只保存引用和非敏感策略。
- composition root 根据 deployment 构造 provider-specific client/model，再注册为 provider-neutral `ModelProvider`；业务 Agent、router 和预算服务不得取得 Pydantic AI/OpenAI 等 SDK 对象。
- runtime 先形成 immutable route plan，再执行 policy/budget/provider；route plan 和 usage evidence 保存 deployment/provider/model/endpoint origin/capability/price/version/attempt 身份，但不保存 credential、Authorization header 或含密 URL。
- 首个 `controlled-real-model-runtime` change 只交付非流式文本 completion；完成并归档后，下一项 P0 行为 change 固定为 Phase 18.1 `controlled-model-streaming`。Provider-neutral structured output、模型驱动工具循环和多 provider 运维继续分别后置，不能借首个真实调用入口或流式入口绕过 `ToolRegistry`、Policy/HITL、预算或 `CanonicalEvent`。

**规则：**
- MUST committed YAML、descriptor、请求、event、trace、audit、eval、error、health 与配置 snapshot 中都不出现秘密原值；`.env` 高于 YAML 的加载优先级不构成安全保证，正式部署不得把提交 `.env` 当 secret store。
- MUST Harness 的真实模型配置使用 `AGENT_HARNESS_*` typed fields 和 credential reference；不得静默读取 `OPENAI_API_KEY` 等 provider 原生 ambient env 形成第二条未审计路径。兼容入口如确需读取，必须在 composition root 显式映射、记录来源类别并通过相同脱敏/冲突门禁。
- MUST `base_url` 缺省时解析到 provider 官方默认 origin；显式值必须拒绝 userinfo、query、fragment 和未批准 scheme/origin。正式 profile 默认只允许 HTTPS exact origin；仅 local profile 可通过显式策略允许 loopback HTTP，且对应 credential 不得被转发到其他 origin。
- MUST deployment allowlist 是部署上限，Agent descriptor 只能收窄，单次 request 只能再次收窄；未知、空交集、能力不匹配或 request 试图覆盖 endpoint/credential 时，在预算预约、授权成功审计和网络副作用前失败。失败路径可以提交去敏的本地拒绝事件与审计，但不得产生 provider-authorized route evidence。
- MUST timeout、retry 与 concurrency 共同受 total deadline 和 Bulkhead 约束。只对配置列明的瞬时网络错误/状态码有限重试，不得在已观察到响应或 side-effect 状态未知后盲目重试；每次 attempt 都要进入 provider-neutral evidence。
- MUST cost hard limit 启用时，route 的 input/output price 与目录版本必须完整、非负且有限；未知价格不能按零成本继续。最大重试可能造成的预算风险必须在 reservation 或 unknown/needs-review 状态中显式表达。
- MUST provider timeout 尽可能使用异步客户端/transport 的可取消 deadline；仅在线程池外等待超时而不能终止已开始网络调用，不得宣称 provider 已取消。无法证明取消时必须标记 side-effect unknown 并禁止自动重复调用。
- MUST 默认 CI、unit、contract、eval 和 `make smoke-local` 继续使用 fake/provider doubles，不访问外网、不需要真实 key；真实 provider smoke 必须显式 opt-in、使用非生产凭据、受 endpoint allowlist 约束，并只报告脱敏结果。

**验收标准：**
- [ ] AC-077: Given profile、Agent YAML、`.env`、secret file、direct env 与受控 overrides 同时提供配置, when 加载 settings, then 按公开优先级确定非冲突值，direct/`_FILE` 冲突先失败，`.env` 中非 `AGENT_HARNESS_*` provider 原生变量不被误当已加载凭据。
- [ ] AC-078: Given 一个真实模型 deployment, when 通过 YAML 与 `AGENT_HARNESS_*` 加载, then deployment/provider/model allowlist、default/fallback、base URL、credential ref、timeouts、retry、concurrency、pricing 和 capability 全部进入 typed settings，任何诊断与序列化都不回显 secret。
- [ ] AC-079: Given `base_url` 含 userinfo/query/fragment、非批准 origin、非显式 local loopback HTTP 或与 credential 绑定 origin 不一致, when application startup, then 在创建 SDK client 或发起 DNS/HTTP 请求前 fail closed，错误只包含安全字段路径和 origin 摘要。
- [ ] AC-080: Given deployment、Agent descriptor 与 request 提供不同模型范围, when 路由一次调用, then immutable route plan 只选择三者交集；未知或越权选择在 budget reservation、授权成功 event/audit 和 provider call 前失败，允许提交不含秘密且不声称已授权的本地拒绝 evidence。
- [ ] AC-081: Given 有效受控 deployment 和显式 opt-in 测试凭据, when 执行一次非流式真实文本 completion, then composition root 注册真实 provider，返回 provider-neutral text/usage/latency/route evidence，业务 Agent 无 vendor import，fake 路径仍可离线运行。
- [ ] AC-082: Given provider 超时、429/可重试 5xx、并发饱和或取消, when 执行调用, then retry/Retry-After/backoff、total deadline、Bulkhead 与 unknown side-effect 语义符合冻结配置，attempt 可审计且不会静默重复预算或调用。
- [ ] AC-083: Given 默认 CI 与 local profile, when 运行 quality/test/eval/smoke-local, then 不读取真实 provider key、不触网且继续使用 fake；给定单独授权和隔离凭据时，opt-in live smoke 才执行真实 endpoint，并把未执行状态准确报告为 skipped/hosted-unverified 而非 PASS。
- [ ] AC-084: Given cost hard limit 启用但 route 价格缺失、credential reference 未解析或 provider capability 未声明, when 请求真实模型, then 在任何 provider 副作用前结构化失败，已有 local event/audit 证据不含 secret，预算状态不被伪装成零成本完成。

### REQ-026: 受控真实模型增量文本流

**优先级：** P0
**关联任务：** TASK-004, TASK-008, TASK-014
**关联流程：** FLOW-007

**用途：**
在 REQ-025 已冻结的 deployment、route、预算和 provider 生命周期上，把真实模型的普通文本增量转换为可持久化、可恢复、可审计的 provider-neutral 事件；复用 REQ-014 已实现的 CanonicalEvent / SSE / CLI 传输，不把 SDK stream 或 HTTP 连接变成第二个运行状态真相源。

**行为：**
- Phase 18.1 `controlled-model-streaming` 只交付普通文本流。Provider adapter 以异步、可取消的 provider-neutral stream seam 输出只追加文本片段和最终结果；SDK event、cursor、resume token、headers、logprobs、reasoning 与 tool-call delta 不进入公共 DTO。
- runtime 在 provider 副作用前冻结 route、最大 token/cost reservation 和受信 event-capacity 上限。SDK 的任意细粒度 token 不得直接形成无界事件；增量按冻结的最大分片数、单片大小与合并策略生成稳定 operation/attempt/chunk identity。
- 正常持久化顺序固定为 `model.request.started → model.output.delta* → model.output.completed → model.usage.updated → run terminal`。`model.output.completed` 必须携带可验证的最终长度、checksum 或 artifact reference；经过同一输出安全与归一化处理后的最终公开文本，必须与已提交公开增量的规范化拼接结果一致。
- RUN-006 与 CLI 只读取已提交 `CanonicalEvent`。`Last-Event-ID` / `after_seq` 只定位 event store 中的后继事件，不传给 provider，也不恢复、重启或重放 provider 调用。

**规则：**
- MUST SSE/CLI subscriber 断线只停止该 reader，不默认取消 durable run/provider。只有显式 run cancellation 或冻结 deadline 才请求 adapter 取消；请求已发送后若无法证明远端停止，attempt 必须标记 interrupted/unknown，保留已提交前缀与 reservation，禁止自动 retry/fallback、伪造 `model.output.completed`、按零 usage/cost 结算或提前发布 run terminal。
- MUST 只在首个增量尚未观察且调用明确未发生时，才允许按 REQ-025 的安全前置失败分类重试；观察或提交任一增量后，任何 retry 都必须拒绝，避免拼接两个生成结果和重复计费。
- MUST 增量内容在公开前通过能跨 chunk 保持状态的脱敏与输出安全边界。若某项输出 guardrail 只能对完整结果判断，则该结果不能先作为公开 speculative delta 提交；不得依赖逐 chunk 独立正则声称跨边界 secret 已被遮蔽。
- MUST provider 读取、event commit 与容量消费使用有界背压；storage/event sink 变慢时不得无限缓存、静默丢片、乱序或绕过预约。具体是等待、受控合并还是显式终止，由 change 契约冻结并在 SQLite/PostgreSQL crash-recovery 中逐值证明。
- MUST partial delta 的字符数、chunk 数或本地估算不冒充可信 provider usage。正常完成使用 provider 最终 usage；取消/未知时只记录 provider 可验证的部分值，其余保持 unknown/needs_review，并遵守共享预算 terminal fencing。
- MUST 分开记录 SSE 握手/已有事件首 frame、provider 首 delta、首个已持久化 model delta 和客户端收到该 delta 的时延。AC-066 的 `<1s` 只约束已有事件的 SSE transport，不对外部 provider 首 token 作统一 SLA。
- MUST 默认 CI、unit、contract、eval 和 local smoke 使用可确定分片、取消、背压与恢复的 fake stream double，不访问外网；真实 provider streaming smoke 仍需显式授权、隔离凭据和受信 endpoint，未运行只能记录 external-blocked/hosted-unverified。

**验收标准：**
- [ ] AC-085: Given 有效的 streaming-capable route 与确定性 provider stream, when 完成一次普通文本流, then 按稳定 operation/attempt/chunk identity 持久化有界且有序的 provider-neutral `model.output.delta`，随后严格按 completed、usage、run terminal 顺序收口；公开增量的规范化拼接结果与经过同一安全处理的最终 public text/checksum 一致，公共 DTO/event 不含 SDK 类型或原始事件。
- [ ] AC-086: Given 已发送请求后发生显式取消、deadline 或结果未知, when provider 不能证明远端已停止, then 已提交文本前缀和 reservation 保留，attempt 进入 interrupted/unknown，未发布虚假的 output completed 或 run terminal，未知 usage/cost 不记零，且不会自动 retry/fallback 或再次调用 provider。
- [ ] AC-087: Given SSE/CLI reader 在流中断线后携带最后可见 cursor 重连, when 继续读取同一 run, then 只按 seq 返回未读的已提交 delta/completion/usage/terminal，断线本身不取消 run，reader 不持有 provider cursor，也不恢复、重启或重放 provider 调用。
- [ ] AC-088: Given provider 产生跨 chunk 敏感模式、超过分片/容量上限或 storage/消费者变慢, when 执行流式调用, then 跨 chunk 安全门禁、受信合并/背压、event capacity 和 envelope 上限按冻结契约 fail closed 或有界推进，无 secret、静默丢片、乱序和无界缓存；默认 fake stream 全面验证，opt-in live smoke 分别报告 provider 首 delta 与首个已提交 delta 时延。

### AI 能力规格

| AI 功能 | 能力类型 | 质量条 | 触发方式 | 不确定时 | 服务降级 |
|---|---|---|---|---|---|
| 后端 agent runtime | agent / 工具调用 | unit/contract/integration/eval/smoke 均通过；run 必须产生 terminal event | 用户通过 API/CLI 自动触发 | 返回错误、保留 trace、可生成 eval draft | fake model/local profile 可运行 |
| RAG assistant 示例 | RAG | approved eval cases 通过；回答必须带引用或说明未找到 | 用户调用示例 agent | 说明不确定并给出处/无出处原因 | 降级 BM25/local retrieval |
| Ticket triage 示例 | 文本理解/结构化输出 | 结构化 schema 校验通过；分类 eval 通过 | 用户调用示例 agent | 输出 unknown/needs_review | fake model 测试 |
| Repo analyst 示例 | 文本理解/文件工具 | 不越过 workspace；长输出走 artifact_ref | 用户调用示例 agent | 请求缩小范围或生成 partial report | 禁用 shell，仅 file read/search |
| Dev assistant 示例 | agent / 工具调用 | 危险动作必须触发 approval；audit 完整 | 用户调用示例 agent | 停在 approval 或返回 policy denial | 禁用危险 tool 或 require_approval |
| Trace -> Eval Case | 文本抽取/规则处理 | draft 不含 secret；approved 必须人工确认 | failed/low-score trace 或 CLI | 标记 needs_review，不自动 approved | 只写 local draft |
| 受控真实文本模型（非流式基线） | 概率性文本生成 | route/secret/budget/usage 合同通过；opt-in live smoke 仅在显式授权和隔离凭据下运行 | deployment 与 Agent 策略共同显式启用 | 未知 route、价格、能力或 side effect 时 fail closed/needs_review，不猜测成功 | 运维者可显式改用 local/fake；真实调用失败时 runtime 不自动切 fake，也不得切换到未批准 endpoint/model |
| 受控真实模型增量文本流 | 概率性文本流 | delta 顺序/final checksum/capacity/跨 chunk 安全/取消/部分 usage/Last-Event-ID 合同通过；真实首 delta smoke 单独 opt-in | streaming-capable route 与调用方共同显式启用 | 已观察 delta 后失败则保留 committed prefix 并进入 interrupted/unknown，不重试或伪造完成 | reader 可重连续读 committed events；运维者可在后续新 run 显式选择 non-stream/fake，不把当前失败流静默切换 |

**AI 护栏（绝不能做）：**
- 绝不能绕过 workspace、policy、approval 直接执行危险动作。
- 绝不能把 secret 写入 trace、eval case、audit log、local/jsonl、错误栈。
- 绝不能默认自动把 failed trace 写入 approved dataset。
- 绝不能把 provider 原始事件直接暴露为公共 API 契约。
- 绝不能在业务 agent 中直接耦合厂商 SDK。
- 绝不能让请求或 Agent descriptor 覆盖 deployment 的 endpoint/credential，或把真实凭据发送到未经批准的 `base_url`。
- 绝不能把 provider timeout 当作已取消证明，也不能在 side-effect unknown 后自动重试并把潜在费用记为零。
- 绝不能把 SSE subscriber 断线当作重新调用 provider 的理由，也不能把 raw token/SDK event 直接映射成无界公共事件。
- 绝不能用逐 chunk 独立脱敏冒充跨 chunk 输出安全；无法在增量公开前证明安全的内容必须延迟到完整结果门禁后发布。

## 6. 数据模型

### 6.1 核心实体

| 实体 | 描述 | 关键字段 |
|---|---|---|
| Tenant | 租户上下文，单租户默认 `default` | tenant_id, name, status |
| Identity | 认证后身份上下文 | user_id, tenant_id, roles, permissions, auth_method |
| AgentDescriptor | agent 注册描述 | agent_id, version, input_schema, output_schema, config_ref |
| Session | 用户会话 | session_id, tenant_id, user_id, agent_id, metadata |
| AgentRun | 一次 agent 运行 | run_id, tenant_id, agent_id, session_id, status, parent_run_id |
| ParentBudgetLedger | parent execution tree 的 durable shared hard-limit owner | tenant_id, budget_owner_run_id, hard_token_limit, hard_cost_limit, cost_enabled, snapshot_id, state_version |
| BudgetOperationClaim | root direct 或 delegation top-level operation 的 immutable claim；预算契约中的 `delegation_claim_id` 唯一指既有 `AgentDelegation.id`，持久化列名为 `delegation_id` | tenant_id, budget_owner_run_id, operation_key, identity_hash, request_fingerprint, reserved/actual impact, side_effect_state, status |
| DelegationBudgetAllocation | delegation 内 child model/embedding operation 的额度分配；预算语义中的 `delegation_claim_id` 与关联的 `AgentDelegation.id`/物理列 `delegation_id` 是同一个值，不是第二套标识 | tenant_id, budget_owner_run_id, delegation_id, usage_call_id, identity_hash, reserved/actual impact, status |
| Checkpoint | durable runtime checkpoint | checkpoint_id, tenant_id, run_id, state_ref, resume_token, created_at |
| Approval | HITL 审批记录 | approval_id, tenant_id, run_id, action, decision, approver_id, status |
| PolicyRule | 权限策略 | rule_id, tenant_id, resource, action, effect, conditions |
| AuditLog | 审计记录 | audit_id, tenant_id, run_id, actor, action, decision, trace_id |
| GuardrailCheck | 输入 / 输出护栏检查摘要 | check_id, tenant_id, run_id, source_ref, trust_level, decision, artifact_ref |
| ContextAssembly | 上下文组装记录 | assembly_id, tenant_id, run_id, input_refs, token_budget, truncation_summary, output_ref |
| ToolInvocation | 工具调用记录 | invocation_id, tenant_id, run_id, tool_name, args_ref, result_ref, status |
| TraceRef | 观测 trace 引用 | trace_id, tenant_id, run_id, provider, local_ref, external_url |
| CanonicalEvent | 规范化事件 | event_id, tenant_id, run_id, seq, type, visibility, payload_ref |
| EvalCase | eval case | case_id, tenant_id, agent_id, status, input_ref, expected_ref |
| EvalRun | eval 执行 | eval_run_id, tenant_id, dataset_id, agent_id, status, score_summary |
| EvalScore | eval 分数 | score_id, tenant_id, eval_run_id, case_id, metric, value, provider_ref |
| EvalDatasetSplit | 可复现实验数据集切分 | split_id, tenant_id, agent_id, dataset, strategy, optimization/holdout/regression case ids, evidence_refs |
| EvalExperiment | 固定 split 上的 baseline/candidate 实验 | experiment_id, tenant_id, split_id, idempotency_key, harness manifests, score summaries, comparison, evidence refs |
| HarnessAcceptance | 每个 experiment 唯一且不可变的人工决策 | acceptance_id, tenant_id, experiment_id, reviewer_id, decision, reason, audit_ref, evidence_refs |
| Artifact | 大内容和产物引用 | artifact_id, tenant_id, run_id, kind, uri, checksum |
| Workspace | per-run 或 per-agent 工作区 | workspace_id, tenant_id, run_id, root_path, policy_ref |
| ReleaseRecord | 版本化 release preview CI artifact；非运行时数据库实体 | schema_version, status, current_version, next_version, tag, changelog_ref, artifacts, commit_sha, decision |

### 6.2 实体关系

| 关系 | 描述 |
|---|---|
| Tenant has many Sessions / AgentRuns / EvalCases | 所有核心数据都按租户隔离 |
| AgentDescriptor has many AgentRuns | 一个 agent 可运行多次 |
| Session has many AgentRuns | 会话内可多次调用 agent |
| AgentRun has many Checkpoints / Approvals / Events / ToolInvocations | 运行过程可恢复、可审计、可回放 |
| AgentRun may have parent AgentRun | 支持受控 delegation |
| Root AgentRun owns one ParentBudgetLedger | `budget_owner_run_id` 必须是同 tenant root run；同一 tree 的 direct/delegation/allocation 共用该 owner |
| ParentBudgetLedger has many BudgetOperationClaims | stable key 定位 row，immutable identity 决定 exact replay 或 conflict |
| Delegation BudgetOperationClaim has many DelegationBudgetAllocations | child allocation 受 top-level reservation 和 parent ledger 双重约束，parent 只应用 top-level 差额以避免双计 |
| TraceRef belongs to AgentRun | trace 与运行关联 |
| EvalCase can originate from TraceRef | failed/low-score trace 生成 draft case |
| EvalRun has many EvalScores | eval run 产生多条指标分数 |
| EvalDatasetSplit has many EvalExperiments | 固定 membership 可复用于 baseline/candidate 对比 |
| EvalExperiment has at most one HarnessAcceptance | 人工验收决策唯一且不可变 |
| Artifact belongs to Tenant and optionally Run/EvalCase | 大内容和证据统一引用 |
| ReleaseRecord references git/release artifacts | 通过版本化 manifest 关联 commit、tag 计划、CHANGELOG preview、release notes、wheel/sdist 与 checksum；不关联运行时 tenant 或数据库表 |

### 6.3 数据规则

- 所有持久化的 runtime、storage、policy、audit、event 和 eval 业务实体 MUST 直接带 `tenant_id`，不得只依赖父实体推导；全局只读配置元数据和未持久化 DTO 可例外。
- `run_id`、`trace_id`、`event_id`、`approval_id`、`artifact_id` MUST 全局唯一。
- `CanonicalEvent.seq` MUST 在同一 `run_id` 内单调递增。
- `AgentRun` terminal status MUST 只能是 completed、failed、cancelled 之一。
- `ParentBudgetLedger.budget_owner_run_id` MUST 非空并 tenant-fenced 指向 root `AgentRun.run_id`；任意 child 必须解析到唯一同租户 root 与唯一 delegation relation，P0 拒绝嵌套、孤儿、循环或跨租户 parent topology。
- Budget claim/allocation 的 reserved、actual、unknown、needs-review 与 `side_effect_state` MUST 原子持久化并参与 terminal fencing；exact replay 不得重新读取余额或重复 reservation，identity conflict 必须在预算、event capacity 和外部副作用前拒绝。
- `0016` 对未封闭 legacy tree 的 backfill MUST 引用与 backfill bundle 分离的 durable immutable source evidence，并逐值验证 snapshot/identity/hash/version；不得由 current config、默认值、reservation、actual 或自证 bundle 推导。Cost-enabled snapshot 的所有必需 model/embedding route price MUST 非 null、非 bool、非负且有限。
- `eval-cases/approved` MUST 只能由审核流程写入。
- Secret MUST 在进入 trace/eval/audit/artifact 前脱敏。
- 外部输入、MCP output、tool output、retrieval chunk MUST 在进入模型上下文前带 `source_ref` 和 `trust_level`。
- ContextAssembly MUST 记录截断、压缩、检索注入和 fallback 决策摘要，完整大内容只能通过 Artifact 引用。
- `PolicyDecision` 和 approval 结果 MUST 写 audit log。
- ReleaseRecord MUST 以版本化 JSON manifest 关联 git tag 计划、commit sha、CHANGELOG preview、release notes 和带 checksum 的 artifacts；`no-release` 路径必须显式记录无发布决策。它只作为 CI/release evidence 归档，不创建 `release_records` 表，也不要求发布预演连接应用数据库。

## 7. 外部依赖

| 编号 | 依赖 | 用途 | 是否必需 | 备注 |
|---|---|---|---:|---|
| DEP-001 | Python 3.12+ | 运行语言 | Yes | P0 默认 |
| DEP-002 | uv | workspace、依赖、lock、build/publish | Yes | P0 使用 `uv build` |
| DEP-003 | Pydantic AI / pydantic-ai-slim | 默认 agent runtime 底座 | Yes | 通过 adapter 隔离 |
| DEP-004 | Pydantic AI Harness / pydantic-ai-harness | 可选 capability library：CodeMode、memory、guardrails、managed prompts、repo/filesystem tools 等 | No | 不作为 P0 必选依赖；只有具体能力块需要时才通过受控 integration boundary 引入并锁版本 |
| DEP-005 | FastAPI | HTTP/SSE API | Yes | service app 接入层 |
| DEP-006 | Typer | CLI | Yes | `agent-harness` CLI |
| DEP-007 | DBOS | P0 durable execution service adapter | Yes for service profile | 通过 runtime adapter |
| DEP-008 | SQLAlchemy 2.0 | ORM | Yes | typed declarative |
| DEP-009 | Alembic | DB migration | Yes | `make migrate` |
| DEP-010 | PostgreSQL | service profile 主存储 | Yes for service profile | checkpoint/session/eval/policy |
| DEP-011 | Redis | service profile durable RunQueue | Yes for service profile | 当前只承担 Streams queue；session cache 属于 P1 可选能力，核心抽象不硬绑 |
| DEP-012 | SQLite | local profile 存储 | Yes for local profile | 本地/CI |
| DEP-013 | OpenTelemetry | 观测底座 | Yes | provider adapter 前的统一协议 |
| DEP-014 | Logfire | 推荐观测/eval provider | No | P0 adapter/recommended |
| DEP-015 | Phoenix | 可选观测/eval provider | No | adapter |
| DEP-016 | Langfuse | 可选观测/eval provider | No | adapter |
| DEP-017 | PGroonga | CJK/multilingual full-text search | No | P0 optional adapter |
| DEP-018 | pgvector | semantic retrieval | No | P0 optional adapter |
| DEP-019 | MCP | 外部工具协议 | No | P0 client support |
| DEP-020 | ruff | lint/format | Yes | quality gate |
| DEP-021 | pyright | typecheck | Yes | quality gate |
| DEP-022 | pytest / pytest-asyncio | tests | Yes | TDD |
| DEP-023 | coverage.py | coverage | Yes | CI evidence |
| DEP-024 | pre-commit | local quality hook | Yes | P0 |
| DEP-025 | python-semantic-release or release-please | release automation | Yes | 具体工具可在 dev plan 决策，能力必须 P0 |
| DEP-026 | Docker Compose | service profile local deps | Yes | Postgres/Redis smoke |

## 8. 非功能需求

| 类别 | 要求 | 优先级 |
|---|---|---|
| 性能 | local profile 单 agent fake run 应在 5 秒内完成；SSE 已有事件首 frame 应在 1 秒内返回；真实模型流必须分别记录 provider 首 delta、首个已持久化 delta 和客户端收到该 delta 的时延，不对外部 provider 复用传输层 `<1s` SLA；tool 输出必须支持截断和 artifact_ref | P0 |
| 安全 | API Key/Bearer Token；PolicyEngine；HITL；workspace sandbox；secret redaction；默认危险动作审批；模型 credential 与 endpoint origin 绑定，禁止向未批准 `base_url` 转发秘密 | P0 |
| 隐私 | secret 不进入 trace/eval/audit/local-jsonl；trace -> eval case 必须脱敏和人工确认 | P0 |
| 兼容性 | macOS/Linux 开发环境；Python 3.12+；GitHub Actions/GitLab CI；PostgreSQL service profile；SQLite local profile | P0 |
| 可靠性 | DBOS durable execution；checkpoint/resume；idempotency key；local/jsonl fallback；CI smoke | P0 |
| 可维护性 | 核心包可 build wheel；adapter contract tests；README/docs/ADR；人与 Agent 共用工程原则/贡献规范；可机械架构规则进入 checker/CI；release automation | P0 |
| 可演进性 | P0 不强制微服务或大爆炸重构，但 API、worker、tool/model、storage、event/observability 必须有稳定接口、窄 change DAG、文件所有权和可跨上下文续接的 living plan | P0 |
| 可测试性 | TDD；unit/contract/integration/eval/smoke；fake providers；CI artifacts | P0 |
| 可访问性 | P0 不做产品 UI；OpenAPI/Redoc 保持默认可访问性 | P1 |
| 合规 | Apache-2.0、NOTICE、license check、引用声明 | P0 |

非功能验收：

- [x] AC-065: Given local profile 与 fake provider, when 从入口创建并完成 single agent run, then 稳定 smoke 记录的总时延小于 5 秒。
- [x] AC-066: Given 已建立 SSE 连接且存在可见事件, when 服务开始流式响应, then 首个 event frame 在 1 秒内返回；测试必须区分握手前错误和握手后错误事件。

## 9. P0 完成定义

P0 完成条件：

- [ ] 所有 P0 requirements 已实现。
- [ ] 所有 P0 acceptance criteria 已通过。
- [ ] 所有 P0 能力块都有 unit/contract/integration/eval/smoke 中至少一种验证证据。
- [x] `packages/agent-harness` 可独立 build wheel/sdist。
- [x] `templates/service-app` 使用 wheel 安装 `agent-harness` 后仍可运行测试和 smoke。
- [x] local profile 可在无真实模型 key、无 SaaS provider 情况下跑通。
- [x] service profile 可通过 Docker Compose 跑 PostgreSQL/Redis smoke。
- [x] Trace -> EvalCaseDraft -> Human Review -> Approved Dataset -> EvalRun -> ScoreSink 闭环跑通。
- [x] Policy/HITL 对默认危险动作生效。
- [x] CanonicalEvent terminal event 唯一性、JSON events `after_seq` resume 与 SSE `Last-Event-ID` resume 测试通过。
- [x] README 和深度文档已覆盖目录边界、扩展方式、安全策略、release process。
- [x] README / architecture docs 已覆盖未来微服务拆分边界；service profile 可验证 API 与 worker 分进程协作。
- [ ] 工程原则、贡献规范和架构演进计划已进入中英文导航，且新引入的可机械依赖规则有 checker/contract/CI 证据。
- [ ] 受控真实文本模型能在显式 deployment、受信 endpoint 和隔离凭据下 opt-in 运行；默认 local/CI 仍离线，secret、route、budget、retry 与 usage 证据满足 REQ-025。
- [ ] 受控真实模型普通文本流能通过既有 CanonicalEvent / SSE / CLI 有界输出并恢复；增量顺序、跨 chunk 安全、容量、取消、部分计量、禁止重放 provider 与时延拆分满足 REQ-026。
- [ ] GitHub Actions 和 GitLab CI 都能跑等价质量门禁。
- [ ] Release automation dry-run 能生成版本、tag、CHANGELOG 预览和 wheel/sdist artifacts。
- [x] Apache-2.0 LICENSE、NOTICE 和 license check 存在。

## 10. 假设与待确认问题

### 10.1 假设

| 编号 | 假设 | 假设依据 | 错误风险 |
|---|---|---|---|
| ASM-001 | 主线是后端服务型 agent 脚手架 | 用户确认前端 UI P0 不做，架构图偏后端服务化 | 如果后续转本地桌面工具，Access/Storage/Policy 设计需调整 |
| ASM-002 | Pydantic AI 是默认生态，Pydantic AI Harness 是可选能力库 | 用户确认架构以 Pydantic AI 生态为主；官方将 `pydantic-ai-harness` 定位为独立 capability library | 如果 Harness 能力成熟到必须进入 P0，需先更新依赖表、import boundary 和 adapter 计划 |
| ASM-003 | DBOS 是 P0 service durable execution 默认 | 用户确认接受 DBOS，Temporal 放 P1 | 如果 DBOS 不满足部署要求，需提前实现 Temporal adapter |
| ASM-004 | PostgreSQL + Redis 是 service profile 默认 | 用户确认 Redis 按建议走 | 如果部署环境不能用 Redis，queue adapter 需提前增强 |
| ASM-005 | PGroonga 可作为 P0 optional adapter | 用户明确知道安装方式并要求 P0 | 如果目标用户环境难安装，doctor/degrade 文档要更强 |
| ASM-006 | Release automation P0 不等于必须公开 PyPI 发布 | 用户要求 P0 能抽成 PyPI 包并 release automation P0 | 如果用户要求公开发布，需补 PyPI token/Trusted Publishing 细节 |
| ASM-007 | README 同时服务 app developer 和 scaffold maintainer | 用户明确两类都要 | 如果 README 过长，需拆分并保留入口导航 |
| ASM-008 | P0 不微服务化，但必须为未来微服务拆分打基础 | 用户确认微服务现在太早但以后必须，P0 需避免后续重构困难 | 如果 P0 边界不清，后续拆服务会重写 API/runtime/tool/model/storage/event 交互 |

### 10.2 待确认问题

| 编号 | 问题 | 是否阻塞 | 备注 |
|---|---|---:|---|
| Q-002 | package registry 是私有 PyPI、GitHub Packages、GitLab Package Registry 还是公开 PyPI | No | P0 支持私有发布路径；公开发布后定 |

已从待确认列表移除的既定事项：Release automation 已选择 `python-semantic-release==10.6.1` 作为 GitHub/GitLab 共用的 Conventional Commits 版本真相，仓库 wrapper 负责 dry-run、promotion 与 registry 安全边界；SQLAlchemy 已采用 async ORM + asyncpg seam；local retrieval 已采用 SQLite FTS/BM25 路径；Logfire/Phoenix/Langfuse 已固定为 provider-neutral adapter contract，外部深集成可分层演进。

## 11. Agent 系统规格

### 11.1 自主性与人在回路

| 动作类别 | 自主级别 | 审批 / 回滚 |
|---|---|---|
| 普通模型调用 | 自动 | 超预算触发 policy |
| 常规只读文件访问 | 自动 | 限 workspace 和 `.agentignore` |
| 文件写入 | 可配置 | 批量写和敏感路径默认 require_approval |
| 删除文件 | 默认 require_approval | 审批记录 + audit；可按策略禁用 |
| Shell 命令 | 默认 require_approval，且 tool 默认 disabled | workspace sandbox、timeout、审计 |
| workspace 外路径访问 | 默认 require_approval / deny | 由 policy 决定 |
| MCP 外部连接 | 默认 require_approval | server/tool allowlist |
| 写 approved eval dataset | 默认 require_approval | 人工确认后入库 |
| 修改权限策略 | 默认 require_approval | 审计必须完整 |
| 对外发送消息/工单/邮件 | 默认 require_approval | P0 可作为示例策略，不做完整产品集成 |

### 11.2 工具与能力集

| 工具 / 能力 | 用途 | 权限级别 | 扩展机制 |
|---|---|---|---|
| FileTool | workspace 文件读写搜索 patch/delete | 读/写，受 policy | 内置 tool adapter |
| ShellTool | 命令执行 | 执行，默认关闭 | 内置 tool adapter |
| MCP Client | 外部工具协议接入 | 由 MCP allowlist + policy 控制 | MCP stdio / HTTP/SSE |
| RetrievalProvider | RAG 检索 | 读索引/文档 | BM25/PGroonga/pgvector adapter |
| EmbeddingProvider | embedding 生成和 cache | 模型调用 | OpenAI-compatible/mock/local |
| ModelRouter | 模型选择、fallback、预算 | 模型调用 | provider adapter |
| InputGuardrail | 输入过滤、注入检测、trust marker | 读输入，写 guardrail trace/audit | policy + redaction + context assembly |
| ContextAssembler | 历史裁剪、检索注入、tool output 截断 | 读上下文和 artifact，引导模型输入 | source/trust/token budget contract |
| TelemetryFacade | trace/metric/event 输出 | 写观测 | local/jsonl/OTel/provider adapter |
| EvalRunner | eval 执行和 score | 读 approved dataset，写 score | local/provider adapter |

### 11.3 上下文与记忆

- 单任务上下文上限与超限处理：P0 支持 context compaction 事件和 memory/retrieval 接口；具体复杂 memory 策略可 P1 深化。
- 上下文组装：P0 由 ContextAssembler 统一处理 system/user/history/retrieval/tool output/artifact refs，业务 agent 不直接拼 prompt。
- 信任边界：外部输入、MCP output、tool output、retrieval chunk 默认不可信；进入上下文前必须保留来源、可信级别和截断记录。
- 预算降级：上下文超预算时先裁剪历史和检索/tool output，再压缩记忆或切换 fallback model；无法安全降级时触发 policy。
- 跨会话记忆：P0 不做自动长期个人记忆；只保留 session/history/checkpoint/eval/artifact 的结构化存储。
- 长任务：P0 通过 durable execution、checkpoint、resume、artifact_ref 避免上下文和事件 payload 膨胀。

### 11.4 编排与多 agent

- P0 支持多 agent 注册、路由、隔离与受控 delegation。
- P0 不做复杂 graph-based multi-agent UI。
- P1 可在当前 orchestrator/adapter seam 后引入 graph-based workflow、handoff 策略、coordinator/specialist 模板和多 agent eval 对比。

### 11.5 评估与可观测

- 评估方式：approved eval cases、adapter contract tests、示例 agent eval、release gate。
- 可观测：CanonicalEvent、OTel mapping、local/jsonl、provider adapter、trace_ref。
- 质量退化发现：failed/low-score detector 生成 draft eval case；eval score 写回 local/provider。

## 12. 资料依据与验证状态

### 12.1 已读取 / 已验证资料

- 项目架构图源文件：`docs/architecture/pydantic-ai-agent-architecture.drawio`、`docs/architecture/agent-harness-technical-architecture.drawio`、`docs/architecture/agent-harness-runtime-trust-boundaries.drawio`、`docs/architecture/agent-harness-deployment-boundaries.drawio`，以 drawio 可编辑版本作为项目内引用。
- 项目架构图 Excalidraw 可编辑版本：`docs/architecture/pydantic-ai-agent-architecture.excalidraw`、`docs/architecture/agent-harness-technical-architecture.excalidraw`、`docs/architecture/agent-harness-runtime-trust-boundaries.excalidraw`、`docs/architecture/agent-harness-deployment-boundaries.excalidraw`。
- 项目架构图 PNG 预览版本：`docs/architecture/pydantic-ai-agent-architecture.png`、`docs/architecture/agent-harness-technical-architecture.png`、`docs/architecture/agent-harness-runtime-trust-boundaries.png`、`docs/architecture/agent-harness-deployment-boundaries.png`，用于人工审阅和多模态模型快速理解；可编辑源仍以 `.drawio` / `.excalidraw` 为准。
- 项目说明：`AGENT-PACK.md`，用于区分当前 Agent Pack 能力包与新脚手架产品。
- Pydantic AI 官方文档：overview、streaming、durable execution、multi-agent、Logfire integration、Harness overview。
- Pydantic AI OpenAI provider 官方文档：确认 provider 原生 `OPENAI_API_KEY`、默认 `https://api.openai.com/v1`、显式 `OpenAIProvider`、自定义 `AsyncOpenAI` client 的 `base_url`/organization/project/retry 以及 model settings 均属于可配置 surface；本项目必须在 adapter/composition boundary 收口这些能力，而不是让业务 Agent 直接构造 SDK 对象。
- Pydantic AI HTTP request retries 官方文档：确认 retry 通过自定义 HTTP transport/client 配置，需显式限定 retryable error、`Retry-After`、backoff、最大等待与尝试次数；本项目在此基础上额外要求总 deadline、预算、attempt evidence 与 side-effect unknown 的 fail-closed 语义。
- 当前仓库配置与模型实现：`packages/agent-harness/src/agent_harness/config/settings.py`、`config/schemas.py`、`runtime/services.py` 与 `adapters/models/pydantic_ai.py`；用于确认现有优先级、`AGENT_HARNESS_*` 边界、fake-only composition 和真实 adapter 配置缺口。
- SQLAlchemy 2.0 官方文档：typed declarative、async ORM。
- SQLModel 官方文档/PyPI：用于 ORM 选型对比，最终不进入核心。
- PGroonga 官方资料：用于确认 CJK/multilingual PostgreSQL full-text search adapter。
- Logfire/Phoenix/Langfuse 官方资料：用于确认 OTel、eval/dataset/score/trace 工作流。
- uv 官方文档：用于确认 build/publish 能力。
- release-please / python-semantic-release 公开文档：用于确认 release automation/tag/CHANGELOG generation 可行性。

### 12.2 明确未使用资料

- 未使用任何泄漏的 Claude Code 源码。
- 未把非官方泄漏信息作为需求或架构依据。

### 12.3 运行验证状态

- 已使用 drawio-skill 的 `validate.py` 验证四张 drawio 源文件结构，当前均为 `0 error(s), 0 warning(s)`；另经 PNG 视觉核验确认连线没有穿过无关节点，图例中的线桥只说明交叉时的非连接语义，不代表当前保留 validator warning。
- 已导出四张架构图 PNG 预览；PNG 用于审阅和快速理解，项目内可编辑真相源仍以 `.drawio` / `.excalidraw` 为准。
- 本文档是需求事实源；当前实现进度、验证证据和已归档变更以 `DEV-PLAN.md` 与 `openspec/specs/` 为准。
