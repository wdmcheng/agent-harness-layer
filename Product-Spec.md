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

- `artifacts/pydantic-ai-agent-architecture.drawio`
- `artifacts/pydantic-ai-agent-architecture.png`

架构图表达：

- 纵向 5 层运行中轴：Access、Runtime、Engine、Tools、Infra。
- 左翼 Eval Gate：分层 eval、release gate、trace 低分样本回流。
- 右翼 Observability：OTel trace/metrics/audit，适配 Logfire / Phoenix / Langfuse。
- 底部工程闭环：线上低分 trace -> eval case -> eval run -> score -> observability provider。

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
| SCOPE-004 | Pydantic AI 默认生态与适配层 | P0 | 默认依赖上游包，不 vendoring 全源码 |
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
| SCOPE-021 | Eval Gate 转换层与 trace/eval 闭环 | P0 | draft -> human review -> approved -> eval -> score sink |
| SCOPE-022 | README 与深度文档 | P0 | 面向 app developer 和 scaffold maintainer 两类读者 |
| SCOPE-023 | TDD 测试结构与质量门禁 | P0 | unit、contract、integration、eval、smoke |
| SCOPE-024 | GitHub Actions + GitLab CI | P0 | 两边跑同一命令集，包含 fake model eval 和 service smoke |
| SCOPE-025 | Release automation / tag / CHANGELOG generation | P0 | SemVer、Conventional Commits、自动版本、tag、release artifact、CHANGELOG |
| SCOPE-026 | Apache-2.0 license 与开源合规 | P0 | LICENSE、NOTICE、引用声明、license check |
| SCOPE-027 | CLI | P0 | Typer，包含 run、doctor、approval、eval、policy、scaffold agent |
| SCOPE-028 | 示例 agent 薄样例 | P0 | RAG、ticket triage、repo analyst、dev assistant，各自验证扩展点 |
| SCOPE-029 | 未来微服务拆分基础 | P0 | P0 不强制微服务部署，但必须定义模块/进程/接口边界，避免后续拆分重构 |

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
| OUT-010 | Vault/KMS 等企业密钥管理 adapter | P0 SecretProvider + env/Docker secret，Vault/KMS 放 P1 |
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
1. 开发者创建 `agent.py`、`tools.py`、`schemas.py`、`config.yaml`、`evals/`。
2. `config.yaml` 声明 `agent_id`、模型策略、预算、工具白名单、eval dataset、delegation edge。
3. `AgentRegistry` 加载并校验 descriptor。
4. 开发者通过 CLI 或 `/api/v1/agents/{agent_id}/runs` 运行 agent。
5. 系统为 run 注入 tenant、identity、policy、budget、trace 和 event stream。

**分支路径：**
- agent config schema 不合法时 registry 拒绝加载。
- agent 未声明工具权限时 tool call 被 policy 拒绝或要求审批。

**边界情况：**
- `agents/*` 不允许直接 import 厂商 adapter。
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
4. CLI 或 HTTP 客户端提交 approve/deny。
5. 审批结果写 audit log，并通过 checkpoint resume run。

**分支路径：**
- deny 时 run 按策略失败或走 fallback。
- 审批超时策略 P1；P0 可保持等待或由用户取消。

**边界情况：**
- 审批记录必须关联 `tenant_id`、`agent_id`、`run_id`、`trace_id`。
- 修改权限策略本身默认 `require_approval`。

**完成状态：**
危险动作不会绕过 policy，审批后 run 可恢复，审计链完整。

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
核心包能自动生成版本、tag、CHANGELOG、wheel/sdist 和 release artifact；模板声明兼容版本范围。

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
- [ ] AC-001: Given 仓库根目录, when 执行 `uv sync`, then workspace 所有 P0 package 可以解析依赖。
- [ ] AC-002: Given `packages/agent-harness`, when 执行 `uv build`, then 生成 wheel/sdist。
- [ ] AC-003: Given 已生成 wheel, when 模板 app 使用 wheel 安装, then tests/smoke 不依赖源码路径也能通过。

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
- [ ] AC-004: Given `agents/examples/*`, when 静态扫描 import, then 不出现直接 import `pydantic_ai`、`logfire`、`dbos`、`langfuse`、`phoenix`。
- [ ] AC-005: Given 上游 adapter 被 fake adapter 替换, when 运行 unit/contract tests, then 核心接口测试仍可通过。

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

**验收标准：**
- [ ] AC-006: Given 模板目录, when 执行 `make dev` with local profile, then FastAPI/CLI 至少一种入口可运行示例 agent。
- [ ] AC-007: Given 模板目录, when 执行 `make smoke-service`, then Docker Compose PostgreSQL/Redis service smoke 通过。

### REQ-004: 配置系统

**优先级：** P0  
**关联任务：** TASK-001, TASK-002, TASK-003  

**用途：**  
统一 `.env`、YAML 和 typed settings，避免业务代码手读配置。

**行为：**
- `.env` 放密钥、连接串、本机开关。
- `configs/profiles/*.yaml` 放环境 profile、provider、storage、observability、policy 默认。
- `agents/*/config.yaml` 放 agent 元数据、预算、工具白名单、eval dataset、delegation edge。
- `agent_harness.config` 负责加载、合并、校验。

**规则：**
- MUST 所有配置有 Pydantic schema 校验。
- MUST 业务 agent 不直接 `open("config.yaml")`。
- MUST 配置校验错误包含字段路径和修复提示。

**验收标准：**
- [ ] AC-008: Given 缺失必填配置, when 启动应用, then 启动失败并输出 schema 错误。
- [ ] AC-009: Given local/service profile, when 加载 settings, then storage、queue、observability、policy 解析到 typed config。

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
- [ ] AC-010: Given local profile, when 执行 migration, then SQLite schema 创建成功。
- [ ] AC-011: Given service profile, when 执行 migration, then PostgreSQL schema 创建成功。
- [ ] AC-012: Given repository contract tests, when 对 SQLite 和 PostgreSQL adapter 运行, then 行为一致。

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
- [ ] AC-013: Given run 触发 approval, when 进程重启后 approve, then run 可从 checkpoint resume。
- [ ] AC-014: Given 同一 idempotency key 重复提交, when 创建 run, then 不产生重复 run。

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

**规则：**
- MUST trace/eval/approval/storage 全部带 `agent_id`。
- MUST delegation edge 在 config/policy 中显式声明。
- MUST parent run 聚合 delegated run usage、trace 和 budget。
- MUST 默认禁止任意 agent 互调。

**验收标准：**
- [ ] AC-015: Given 两个 agent, when 未声明 delegation edge, then agent A 调用 agent B 被 policy 拒绝。
- [ ] AC-016: Given 已声明 delegation edge, when agent A 委派 agent B, then usage/budget/trace 归并到 parent run。

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
- [ ] AC-017: Given OpenAPI schema, when 运行 schema 测试, then P0 endpoints 均存在。
- [ ] AC-018: Given CLI, when 执行 `agent-harness doctor`, then 输出 profile、storage、queue、observability、eval 目录状态。

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
- [ ] AC-019: Given 未配置多租户, when 创建 run, then run/session/trace/eval 均带 `tenant_id="default"`。
- [ ] AC-020: Given 无效 Bearer Token, when 调用 P0 API, then 返回认证错误且不创建 run。

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
- 默认策略 provider 支持 YAML 和数据库 provider。
- CLI/HTTP 审批入口支持 approve/deny。

**拦截点：**
- agent run 启动前
- tool 调用前
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

**验收标准：**
- [ ] AC-021: Given shell tool 默认策略, when agent 请求执行 shell, then 返回 `approval.required`。
- [ ] AC-022: Given 审批通过, when resume run, then 原 tool call 继续执行且 audit log 记录审批人和结果。
- [ ] AC-023: Given 策略为 deny, when 执行动作, then 动作不执行且 audit log 记录拒绝。

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

**规则：**
- MUST 工具入参出参 schema 校验。
- MUST 大 tool output 走 artifact/ref，不直接塞进事件正文。
- MUST delete、批量写、workspace 外访问默认 require_approval。
- MUST 支持 `.agentignore`。

**验收标准：**
- [ ] AC-024: Given workspace 外路径, when FileTool read, then 默认拒绝或要求审批。
- [ ] AC-025: Given MCP tool 未在 allowlist, when agent 调用, then policy 拒绝。
- [ ] AC-026: Given shell 输出超过上限, when tool 完成, then stdout/stderr 被截断且 artifact_ref 可用。

### REQ-012: 模型、预算与 embedding

**优先级：** P0  
**关联任务：** TASK-003, TASK-004  

**用途：**  
统一模型路由、fallback、成本、超时和 embedding 调用。

**行为：**
- `ModelRouter` 支持默认模型、任务级模型、fallback、timeout、成本预算。
- `EmbeddingProvider` 支持 OpenAI-compatible embedding API。
- P0 提供 local/mock embedding，用于测试和 CI。
- P0 提供 embedding cache。

**规则：**
- MUST 业务 agent 不直接写 provider 细节。
- MUST 模型/embedding 调用记录 token、cost、latency trace。
- MUST 单次模型调用预计超过预算阈值时触发 policy。
- SHOULD 支持 cheap/flagship/local model routing。

**验收标准：**
- [ ] AC-027: Given fake model provider, when 运行 tests/eval, then 不需要真实 API key。
- [ ] AC-028: Given 预算阈值, when 模型调用预计超阈值, then 产生 policy decision。
- [ ] AC-029: Given 重复 embedding 输入, when 第二次调用, then 命中 cache 或记录 cache miss 原因。

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
- SHOULD OpenSearch/Elasticsearch/Vespa 放 P1/P2。

**验收标准：**
- [ ] AC-030: Given local profile, when RAG 示例检索, then 不依赖 PostgreSQL 扩展也能返回结果。
- [ ] AC-031: Given service profile 且 PGroonga 未安装, when doctor, then 输出降级提示而不是启动崩溃。
- [ ] AC-032: Given hybrid retrieval adapter, when 提供 BM25/vector 结果, then 可执行 RRF 合并。

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
reasoning.delta
tool.call.args_delta
tool.call.started
tool.call.completed
tool.call.failed
retrieval.query.started
retrieval.query.completed
policy.decision
approval.required
approval.resolved
checkpoint.created
context.compaction.started
context.compaction.completed
eval.case.drafted
eval.case.approved
eval.run.started
eval.run.completed
eval.score.recorded
```

**规则：**
- MUST 每个 run 内 `seq` 单调递增。
- MUST terminal event 且只能有一个：`run.completed` / `run.failed` / `run.cancelled`。
- MUST `tool.call.args_delta` 可表示半截 JSON，不要求每个 delta 可解析。
- MUST `reasoning.delta` 默认不对普通用户暴露。
- MUST hook/policy/approval 控制事件进 audit/local-jsonl，即使不推给前端。
- MUST 大 payload 走 `payload_ref`。
- MUST SSE 是输出协议，不是内部事件模型。

**验收标准：**
- [ ] AC-033: Given 一个 run, when event stream 完成, then terminal event 只有一个。
- [ ] AC-034: Given SSE 客户端断开后按 seq 恢复, when 重新连接, then 可继续获取未读事件。
- [ ] AC-035: Given 普通用户 visibility, when reasoning event 产生, then 默认不发送给用户流。

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
- [ ] AC-036: Given 未配置任何 SaaS provider, when 运行 agent, then local/jsonl 仍产出 trace。
- [ ] AC-037: Given 配置 Logfire adapter, when 运行 agent, then provider adapter contract test 通过且业务代码无 Logfire import。

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
- P1 支持满足规则后自动入库，默认关闭。

**流程：**

```text
Runtime Trace
  -> TraceCollector
  -> Failure / Low-score Detector
  -> EvalCaseDraft
  -> Human Review Queue
  -> Eval Dataset
  -> Eval Runner
  -> ScoreSink
  -> Observability Provider
```

**规则：**
- MUST `eval-cases/drafts` 和 `eval-cases/approved` 分离。
- MUST `make eval` 只跑 approved cases。
- MUST approved dataset 写入默认 require_approval。
- MUST draft 到 approved 的过程检查 secret/隐私脱敏。
- SHOULD P1 接入 Logfire Hosted Datasets、Phoenix dataset/eval workflow、Langfuse annotation/dataset/score。

**验收标准：**
- [ ] AC-038: Given failed run trace, when 执行 `agent-harness eval draft`, then 生成 draft case。
- [ ] AC-039: Given draft case, when 人工 approve, then case 进入 approved dataset 并写 audit log。
- [ ] AC-040: Given approved dataset, when `make eval`, then 产出 eval result 和 score sink 记录。

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
- [ ] AC-041: Given P0 示例 agent, when `agent-harness agents list`, then 四个示例均可见。
- [ ] AC-042: Given 每个示例, when 执行对应 eval, then fake model 下可跑通确定性测试。

### REQ-018: README 与文档体系

**优先级：** P0  
**关联任务：** TASK-001, TASK-008, TASK-011  

**用途：**  
README 是入口，深度文档解释架构和维护边界。

**README MUST 包含：**
- What this scaffold is
- Quick Start
- Project Structure
- For Agent App Developers
- For Scaffold Maintainers
- Deep Docs
- License & Compliance
- Release Process

**深度文档 MUST 包含：**
- `docs/architecture.md`
- `docs/extension-guide.md`
- `docs/adapter-contracts.md`
- `docs/eval-observability-loop.md`
- `docs/security-policy.md`
- `docs/release-process.md`
- `docs/adr/`

**目录边界 MUST 写入 README：**
- `agents/*` 不直接 import 厂商 SDK，只走 `agent_harness`。
- `app/*` 不写业务 agent 逻辑，只负责协议入口和响应转换。
- `agent_harness/*` 不依赖具体示例 agent。
- `adapters/*` 可以依赖外部厂商包，但不能反向污染核心接口。
- `eval-cases/approved` 只能由审核流程写入。
- 所有运行记录必须带 `tenant_id`、`agent_id`、`run_id`。
- 多 agent delegation 必须走 registry 和 policy。

**验收标准：**
- [ ] AC-043: Given README, when 新开发者阅读 Project Structure, then 能知道每个目录职责和禁止跨边界规则。
- [ ] AC-044: Given scaffold maintainer, when 阅读 docs, then 能找到 adapter contract、release process、安全策略和 ADR。

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
- [ ] AC-045: Given 新能力块任务, when 开发开始, then 先存在失败测试或 contract test。
- [ ] AC-046: Given `make quality`, when CI 执行, then ruff、pyright、unit/contract tests 均通过。
- [ ] AC-047: Given `make eval`, when 未配置真实模型 key, then fake model eval 可通过。

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
- MUST template 声明兼容的 `agent-harness` 版本范围，例如 `>=0.1,<0.2`。
- MUST 破坏性变更写 ADR。

**验收标准：**
- [ ] AC-048: Given GitHub CI, when push/PR, then `make quality`、`make eval`、`make smoke-local`、`make smoke-service` 执行。
- [ ] AC-049: Given GitLab CI, when pipeline, then 与 GitHub 等价命令通过。
- [ ] AC-050: Given releasable commits, when release workflow dry-run, then 生成下一版本、CHANGELOG 预览、tag 名称和 wheel/sdist artifact。
- [ ] AC-051: Given no releasable commits, when release workflow dry-run, then 不创建 tag 或 release。

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
- [ ] AC-052: Given 仓库根目录, when 检查 license 文件, then `LICENSE` 存在且为 Apache-2.0。
- [ ] AC-053: Given 引入第三方片段, when review, then NOTICE/来源/license/修改说明可追踪。

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
- P0 MUST 工具执行、模型调用、存储访问、事件输出都通过 adapter/provider/facade，不允许业务 agent 直接依赖具体实现。
- P0 SHOULD 定义 `EventBus` / `EventSink` 抽象；默认实现可以是 local/jsonl、DBOS/Redis queue 或进程内测试 adapter，Kafka/RabbitMQ/NATS 放 P1/P2 adapter。
- P0 SHOULD 在 DEV-PLAN 中写明未来拆分顺序，默认先拆 worker，再拆 tool/model gateway，最后拆 observability/event pipeline。

**规则：**
- MUST P0 不把物理微服务、服务注册发现、WAF、Kubernetes、多 AZ 当作必交付功能。
- MUST README / `docs/architecture.md` 解释哪些模块今天同进程、哪些边界未来可拆、拆分时哪些接口不变。
- MUST CI 至少包含一条 service profile smoke，验证 API 进程和 worker 进程使用同一 storage/queue 配置时可协作。
- MUST 禁止为了图上好看提前引入分布式复杂度；边界优先，分布式实现后置。

**验收标准：**
- [ ] AC-054: Given README / architecture docs, when 新维护者阅读部署边界章节, then 能指出 API、runtime worker、model/tool gateway、storage、event pipeline 的当前形态和未来拆分路径。
- [ ] AC-055: Given service profile, when 分别启动 API 进程和 worker 进程并提交 run, then run 可被 worker 执行并通过共享 storage/queue 产出事件。
- [ ] AC-056: Given 业务 agent 代码, when 静态扫描 import, then 不直接 import 具体 model/tool/storage/observability vendor SDK 或直接操作 ORM session。
- [ ] AC-057: Given CanonicalEvent / DTO contract tests, when API、worker、tool/model adapter 交换数据, then 关联字段和 schema 校验保持一致。

### AI 能力规格

| AI 功能 | 能力类型 | 质量条 | 触发方式 | 不确定时 | 服务降级 |
|---|---|---|---|---|---|
| 后端 agent runtime | agent / 工具调用 | unit/contract/integration/eval/smoke 均通过；run 必须产生 terminal event | 用户通过 API/CLI 自动触发 | 返回错误、保留 trace、可生成 eval draft | fake model/local profile 可运行 |
| RAG assistant 示例 | RAG | approved eval cases 通过；回答必须带引用或说明未找到 | 用户调用示例 agent | 说明不确定并给出处/无出处原因 | 降级 BM25/local retrieval |
| Ticket triage 示例 | 文本理解/结构化输出 | 结构化 schema 校验通过；分类 eval 通过 | 用户调用示例 agent | 输出 unknown/needs_review | fake model 测试 |
| Repo analyst 示例 | 文本理解/文件工具 | 不越过 workspace；长输出走 artifact_ref | 用户调用示例 agent | 请求缩小范围或生成 partial report | 禁用 shell，仅 file read/search |
| Dev assistant 示例 | agent / 工具调用 | 危险动作必须触发 approval；audit 完整 | 用户调用示例 agent | 停在 approval 或返回 policy denial | 禁用危险 tool 或 require_approval |
| Trace -> Eval Case | 文本抽取/规则处理 | draft 不含 secret；approved 必须人工确认 | failed/low-score trace 或 CLI | 标记 needs_review，不自动 approved | 只写 local draft |

**AI 护栏（绝不能做）：**
- 绝不能绕过 workspace、policy、approval 直接执行危险动作。
- 绝不能把 secret 写入 trace、eval case、audit log、local/jsonl、错误栈。
- 绝不能默认自动把 failed trace 写入 approved dataset。
- 绝不能把 provider 原始事件直接暴露为公共 API 契约。
- 绝不能在业务 agent 中直接耦合厂商 SDK。

## 6. 数据模型

### 6.1 核心实体

| 实体 | 描述 | 关键字段 |
|---|---|---|
| Tenant | 租户上下文，单租户默认 `default` | tenant_id, name, status |
| Identity | 认证后身份上下文 | user_id, tenant_id, roles, permissions, auth_method |
| AgentDescriptor | agent 注册描述 | agent_id, version, input_schema, output_schema, config_ref |
| Session | 用户会话 | session_id, tenant_id, user_id, agent_id, metadata |
| AgentRun | 一次 agent 运行 | run_id, tenant_id, agent_id, session_id, status, parent_run_id |
| Checkpoint | durable runtime checkpoint | checkpoint_id, run_id, state_ref, resume_token, created_at |
| Approval | HITL 审批记录 | approval_id, run_id, action, decision, approver_id, status |
| PolicyRule | 权限策略 | rule_id, tenant_id, resource, action, effect, conditions |
| AuditLog | 审计记录 | audit_id, tenant_id, run_id, actor, action, decision, trace_id |
| ToolInvocation | 工具调用记录 | invocation_id, run_id, tool_name, args_ref, result_ref, status |
| TraceRef | 观测 trace 引用 | trace_id, run_id, provider, local_ref, external_url |
| CanonicalEvent | 规范化事件 | event_id, run_id, seq, type, visibility, payload_ref |
| EvalCase | eval case | case_id, tenant_id, agent_id, status, input_ref, expected_ref |
| EvalRun | eval 执行 | eval_run_id, dataset_id, agent_id, status, score_summary |
| EvalScore | eval 分数 | score_id, eval_run_id, case_id, metric, value, provider_ref |
| Artifact | 大内容和产物引用 | artifact_id, tenant_id, run_id, kind, uri, checksum |
| Workspace | per-run 或 per-agent 工作区 | workspace_id, tenant_id, run_id, root_path, policy_ref |
| ReleaseRecord | release automation 记录 | version, tag, changelog_ref, artifacts, commit_sha |

### 6.2 实体关系

| 关系 | 描述 |
|---|---|
| Tenant has many Sessions / AgentRuns / EvalCases | 所有核心数据都按租户隔离 |
| AgentDescriptor has many AgentRuns | 一个 agent 可运行多次 |
| Session has many AgentRuns | 会话内可多次调用 agent |
| AgentRun has many Checkpoints / Approvals / Events / ToolInvocations | 运行过程可恢复、可审计、可回放 |
| AgentRun may have parent AgentRun | 支持受控 delegation |
| TraceRef belongs to AgentRun | trace 与运行关联 |
| EvalCase can originate from TraceRef | failed/low-score trace 生成 draft case |
| EvalRun has many EvalScores | eval run 产生多条指标分数 |
| Artifact belongs to Tenant and optionally Run/EvalCase | 大内容和证据统一引用 |

### 6.3 数据规则

- 所有核心实体 MUST 带 `tenant_id`，除非是全局只读元数据。
- `run_id`、`trace_id`、`event_id`、`approval_id`、`artifact_id` MUST 全局唯一。
- `CanonicalEvent.seq` MUST 在同一 `run_id` 内单调递增。
- `AgentRun` terminal status MUST 只能是 completed、failed、cancelled 之一。
- `eval-cases/approved` MUST 只能由审核流程写入。
- Secret MUST 在进入 trace/eval/audit/artifact 前脱敏。
- `PolicyDecision` 和 approval 结果 MUST 写 audit log。
- ReleaseRecord MUST 可关联 git tag、commit sha、CHANGELOG 和 artifacts。

## 7. 外部依赖

| 编号 | 依赖 | 用途 | 是否必需 | 备注 |
|---|---|---|---:|---|
| DEP-001 | Python 3.12+ | 运行语言 | Yes | P0 默认 |
| DEP-002 | uv | workspace、依赖、lock、build/publish | Yes | P0 使用 `uv build` |
| DEP-003 | Pydantic AI / pydantic-ai-slim | 默认 agent runtime 底座 | Yes | 通过 adapter 隔离 |
| DEP-004 | FastAPI | HTTP/SSE API | Yes | service app 接入层 |
| DEP-005 | Typer | CLI | Yes | `agent-harness` CLI |
| DEP-006 | DBOS | P0 durable execution service adapter | Yes for service profile | 通过 runtime adapter |
| DEP-007 | SQLAlchemy 2.0 | ORM | Yes | typed declarative |
| DEP-008 | Alembic | DB migration | Yes | `make migrate` |
| DEP-009 | PostgreSQL | service profile 主存储 | Yes for service profile | checkpoint/session/eval/policy |
| DEP-010 | Redis | service profile queue/cache | Yes for service profile | 核心抽象不硬绑 |
| DEP-011 | SQLite | local profile 存储 | Yes for local profile | 本地/CI |
| DEP-012 | OpenTelemetry | 观测底座 | Yes | provider adapter 前的统一协议 |
| DEP-013 | Logfire | 推荐观测/eval provider | No | P0 adapter/recommended |
| DEP-014 | Phoenix | 可选观测/eval provider | No | adapter |
| DEP-015 | Langfuse | 可选观测/eval provider | No | adapter |
| DEP-016 | PGroonga | CJK/multilingual full-text search | No | P0 optional adapter |
| DEP-017 | pgvector | semantic retrieval | No | P0 optional adapter |
| DEP-018 | MCP | 外部工具协议 | No | P0 client support |
| DEP-019 | ruff | lint/format | Yes | quality gate |
| DEP-020 | pyright | typecheck | Yes | quality gate |
| DEP-021 | pytest / pytest-asyncio | tests | Yes | TDD |
| DEP-022 | coverage.py | coverage | Yes | CI evidence |
| DEP-023 | pre-commit | local quality hook | Yes | P0 |
| DEP-024 | python-semantic-release or release-please | release automation | Yes | 具体工具可在 dev plan 决策，能力必须 P0 |
| DEP-025 | Docker Compose | service profile local deps | Yes | Postgres/Redis smoke |

## 8. 非功能需求

| 类别 | 要求 | 优先级 |
|---|---|---|
| 性能 | local profile 单 agent fake run 应在 5 秒内完成；SSE 首事件应在 1 秒内返回；tool 输出必须支持截断和 artifact_ref | P0 |
| 安全 | API Key/Bearer Token；PolicyEngine；HITL；workspace sandbox；secret redaction；默认危险动作审批 | P0 |
| 隐私 | secret 不进入 trace/eval/audit/local-jsonl；trace -> eval case 必须脱敏和人工确认 | P0 |
| 兼容性 | macOS/Linux 开发环境；Python 3.12+；GitHub Actions/GitLab CI；PostgreSQL service profile；SQLite local profile | P0 |
| 可靠性 | DBOS durable execution；checkpoint/resume；idempotency key；local/jsonl fallback；CI smoke | P0 |
| 可维护性 | 核心包可 build wheel；adapter contract tests；README/docs/ADR；release automation | P0 |
| 可演进性 | P0 不强制微服务，但 API、worker、tool/model、storage、event/observability 必须有稳定接口和未来拆分路径 | P0 |
| 可测试性 | TDD；unit/contract/integration/eval/smoke；fake providers；CI artifacts | P0 |
| 可访问性 | P0 不做产品 UI；OpenAPI/Redoc 保持默认可访问性 | P1 |
| 合规 | Apache-2.0、NOTICE、license check、引用声明 | P0 |

## 9. P0 完成定义

P0 完成条件：

- [ ] 所有 P0 requirements 已实现。
- [ ] 所有 P0 acceptance criteria 已通过。
- [ ] 所有 P0 能力块都有 unit/contract/integration/eval/smoke 中至少一种验证证据。
- [ ] `packages/agent-harness` 可独立 build wheel/sdist。
- [ ] `templates/service-app` 使用 wheel 安装 `agent-harness` 后仍可运行测试和 smoke。
- [ ] local profile 可在无真实模型 key、无 SaaS provider 情况下跑通。
- [ ] service profile 可通过 Docker Compose 跑 PostgreSQL/Redis smoke。
- [ ] Trace -> EvalCaseDraft -> Human Review -> Approved Dataset -> EvalRun -> ScoreSink 闭环跑通。
- [ ] Policy/HITL 对默认危险动作生效。
- [ ] CanonicalEvent terminal event 唯一性和 seq resume 测试通过。
- [ ] README 和深度文档已覆盖目录边界、扩展方式、安全策略、release process。
- [ ] README / architecture docs 已覆盖未来微服务拆分边界；service profile 可验证 API 与 worker 分进程协作。
- [ ] GitHub Actions 和 GitLab CI 都能跑等价质量门禁。
- [ ] Release automation dry-run 能生成版本、tag、CHANGELOG 预览和 wheel/sdist artifacts。
- [ ] Apache-2.0 LICENSE、NOTICE 和 license check 存在。

## 10. 假设与待确认问题

### 10.1 假设

| 编号 | 假设 | 假设依据 | 错误风险 |
|---|---|---|---|
| ASM-001 | 主线是后端服务型 agent 脚手架 | 用户确认前端 UI P0 不做，架构图偏后端服务化 | 如果后续转本地桌面工具，Access/Storage/Policy 设计需调整 |
| ASM-002 | Pydantic AI 是默认生态 | 用户确认架构以 Pydantic AI 生态为主 | 如果上游重大变动，需 adapter/fork 决策 |
| ASM-003 | DBOS 是 P0 service durable execution 默认 | 用户确认接受 DBOS，Temporal 放 P1 | 如果 DBOS 不满足部署要求，需提前实现 Temporal adapter |
| ASM-004 | PostgreSQL + Redis 是 service profile 默认 | 用户确认 Redis 按建议走 | 如果部署环境不能用 Redis，queue adapter 需提前增强 |
| ASM-005 | PGroonga 可作为 P0 optional adapter | 用户明确知道安装方式并要求 P0 | 如果目标用户环境难安装，doctor/degrade 文档要更强 |
| ASM-006 | Release automation P0 不等于必须公开 PyPI 发布 | 用户要求 P0 能抽成 PyPI 包并 release automation P0 | 如果用户要求公开发布，需补 PyPI token/Trusted Publishing 细节 |
| ASM-007 | README 同时服务 app developer 和 scaffold maintainer | 用户明确两类都要 | 如果 README 过长，需拆分并保留入口导航 |
| ASM-008 | P0 不微服务化，但必须为未来微服务拆分打基础 | 用户确认微服务现在太早但以后必须，P0 需避免后续重构困难 | 如果 P0 边界不清，后续拆服务会重写 API/runtime/tool/model/storage/event 交互 |

### 10.2 待确认问题

| 编号 | 问题 | 是否阻塞 | 备注 |
|---|---|---:|---|
| Q-001 | release automation 具体选择 python-semantic-release、release-please，还是双 CI 分别适配 | No | DEV-PLAN 阶段决策；P0 能力已确定 |
| Q-002 | package registry 是私有 PyPI、GitHub Packages、GitLab Package Registry 还是公开 PyPI | No | P0 支持私有发布路径；公开发布后定 |
| Q-003 | SQLAlchemy async/sync 具体策略 | No | 建议 async ORM + asyncpg；DEV-PLAN 细化 |
| Q-004 | local BM25 使用具体实现 | No | 可在 DEV-PLAN 阶段评估 SQLite FTS/BM25 或 Python 库 |
| Q-005 | Logfire/Phoenix/Langfuse adapter P0 深度 | No | P0 至少 contract + local/jsonl；外部深集成可分层 |

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
| TelemetryFacade | trace/metric/event 输出 | 写观测 | local/jsonl/OTel/provider adapter |
| EvalRunner | eval 执行和 score | 读 approved dataset，写 score | local/provider adapter |

### 11.3 上下文与记忆

- 单任务上下文上限与超限处理：P0 支持 context compaction 事件和 memory/retrieval 接口；具体复杂 memory 策略可 P1 深化。
- 跨会话记忆：P0 不做自动长期个人记忆；只保留 session/history/checkpoint/eval/artifact 的结构化存储。
- 长任务：P0 通过 durable execution、checkpoint、resume、artifact_ref 避免上下文和事件 payload 膨胀。

### 11.4 编排与多 agent

- P0 支持多 agent 注册、路由、隔离与受控 delegation。
- P0 不做复杂 graph-based multi-agent UI。
- P1 支持 graph-based workflow、handoff 策略、coordinator/specialist 模板和多 agent eval 对比。

### 11.5 评估与可观测

- 评估方式：approved eval cases、adapter contract tests、示例 agent eval、release gate。
- 可观测：CanonicalEvent、OTel mapping、local/jsonl、provider adapter、trace_ref。
- 质量退化发现：failed/low-score detector 生成 draft eval case；eval score 写回 local/provider。

## 12. 资料依据与验证状态

### 12.1 已读取 / 已验证资料

- 项目架构图：`artifacts/pydantic-ai-agent-architecture.drawio`，已导出 PNG 并通过 drawio validate。
- 项目说明：`AGENT-PACK.md`，用于区分当前 Agent Pack 能力包与新脚手架产品。
- Pydantic AI 官方文档：overview、streaming、durable execution、multi-agent、Logfire integration、Harness overview。
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

- 已验证 drawio 源文件结构：`0 error(s), 0 warning(s)`。
- 已导出架构图 PNG：`artifacts/pydantic-ai-agent-architecture.png`。
- 尚未实现代码；本文档是需求事实源，不是运行结果。
