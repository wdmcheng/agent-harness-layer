# Development Plan - Agent Harness Layer

> 本文件记录 Agent Harness Layer 的开发阶段划分、当前进度和剩余工作。
> 新 session 启动时应首先阅读 `Product-Spec.md`、本文件和最新 git 状态，再继续开发。

---

## 当前状态

- Product Spec: `Product-Spec.md` 已存在，版本为 2026-07-05 的 v1.0。
- Design Brief: 未提供。P0 不做产品化前端 UI，本计划按后端脚手架、架构图和既有 Spec 降级规划。
- 设计稿 / 架构图: 已读取 `artifacts/pydantic-ai-agent-architecture.drawio`，按 5 层运行中轴、Agent Loop / HITL / 流式回边、Eval Gate、Observability、信任边界和未来拆分边界组织开发顺序。
- API Contract: `API-Contract.md` 已补入。由于 P0 不做产品化前端 UI，契约按入口 / 调用方映射 CLI、OpenAPI 调用方、service-app、worker 和未来 Access/API gateway；新增或修改 HTTP endpoint 前必须先更新契约，再做局部 OpenAPI 漂移检查。
- OpenSpec: 仓库存在 `openspec/`；Phase 1 的 `bootstrap-workspace-packaging`、Phase 2 的 `core-config-identity-contracts`、Phase 3 的 `storage-migration-uow`、Phase 4 的 `canonical-events-artifacts`、Phase 5 的 `runtime-checkpoint-runs` 均已归档，并同步为主规格。
- 代码状态: Phase 1-2 已完成并提交；Phase 3-5 的 storage/migration/UoW、CanonicalEvent/artifact/local telemetry、runtime/checkpoint/run lifecycle 已完成实现、验证、code-review 和 OpenSpec 归档，等待本轮本地提交。
- 计划模式: 迭代模式。已完成 Phase 保持冻结，只更新状态、剩余工作、风险和后续 Phase 入口。

## 当前进度

| 项目 | 状态 | 证据 / 下一步 |
|------|------|---------------|
| 总体状态 | 进行中 | Phase 1-5 已实现并通过本地验证；Phase 6-15 仍待实现。 |
| 当前 Phase | Phase 5 完成，准备本地提交 | `storage-migration-uow`、`canonical-events-artifacts`、`runtime-checkpoint-runs` 已归档到 `openspec/changes/archive/2026-07-06-*`，主规格已同步。 |
| 已完成 Phase | Phase 1, Phase 2, Phase 3, Phase 4, Phase 5 | Phase 1 实现提交 `c08191b`，安装修复提交 `4ec5c40`，归档提交 `87cf84b`；Phase 2 实现提交 `07fa8da`。Phase 3-5 尚待本轮提交。 |
| 当前 OpenSpec change | 无 active change | `openspec list` 输出 `No active changes found.`；归档后 `openspec validate --all --strict` 为 10 passed。 |
| 当前验证基线 | 全量通过 | 本轮已通过 `uv sync`、`make quality`、`make test`（39 passed, 1 skipped）、`make smoke-local`、`make smoke-service`（含 `worker_run`）、PostgreSQL repository contract、`make build`、`make license-check`、`uv run pre-commit run --all-files`。 |
| 当前阻塞项 | 无 | 剩余是本轮本地提交。 |
| 当前建议下一步 | 启动 Phase 6 proposal | 本轮提交后，从 Phase 6 Agent Registry、模型路由与 Embedding 开始新的窄 OpenSpec change。 |

## 剩余工作

### 立即下一步

- 完成本轮本地提交。
- 下一轮为 Phase 6 创建新的窄 OpenSpec change：Agent Registry、模型路由与 Embedding。
- Phase 6 proposal 开工前，先把 `API-Contract.md` 中 `AGT-001` 扩展为完整 endpoint 条目，并新增 `/api/v1/agents` 的局部 OpenAPI 漂移检查。

### 后续 Phase

- Phase 6: Agent Registry、模型路由与 Embedding。
- Phase 7: 认证、PolicyEngine 与 HITL 审批。
- Phase 8: ToolRegistry、FileTool、ShellTool 与 MCP Client。
- Phase 9: RetrievalProvider 与 RAG 能力。
- Phase 10: Observability Provider Adapters 与脱敏。
- Phase 11: Eval Gate 与 Trace 到 Eval 闭环。
- Phase 12: Service App 模板与四个 P0 示例 Agent。
- Phase 13: Service Profile、API/Worker 分进程与未来拆分边界。
- Phase 14: 深度文档、ADR 与维护者指南。
- Phase 15: CI/CD、Release Automation 与合规收口。

### 尚未完成的关键验收

- policy、tools、retrieval、provider observability adapter、eval gate 和 release automation 尚未实现。
- GitHub Actions / GitLab CI、CHANGELOG/tag/release dry-run 尚未实现。
- 深度文档、ADR、未来微服务拆分边界文档尚未完成。

## 技术栈决策

以下版本于 2026-07-05 通过官方文档、PyPI、GitHub Release 或项目官网核验。开发时使用 `uv.lock` 锁定实际解析版本；本表给出 P0 目标线和上限策略。

| 层级 | 技术 | 版本 | 说明 |
|------|------|------|------|
| 运行语言 | Python | `>=3.12`，CI 首批跑 3.12 和 3.13 | Spec 要求 Python 3.12+；Pydantic AI 2.5.0 PyPI 元数据覆盖 Python 3.10-3.13，因此 P0 先把 3.12/3.13 作为强门禁。 |
| 包管理 / Workspace | uv | `0.11.26` | GitHub Release 2026-06-30 最新稳定版；负责 workspace、lock、build、publish。 |
| Build backend | hatchling | `1.30.1` | 现代 Python build backend，配合 `uv build` 产出 wheel/sdist。 |
| Agent runtime 底座 | pydantic-ai / pydantic-ai-slim | `2.5.0` | 默认底座，但业务 agent 只依赖 `agent_harness` 公共接口；优先使用 slim + extras 降低依赖面。 |
| 数据校验 | Pydantic | `2.13.4` | 配置、DTO、API schema、CanonicalEvent 和 adapter contract 的统一 schema 基础。 |
| HTTP API | FastAPI | `0.139.0` | 实现 `/api/v1/...`、OpenAPI、Swagger、Redoc 和 SSE endpoint。 |
| ASGI Server | Uvicorn | `0.50.0` | service app 本地 API 入口；开发态使用 `uvicorn[standard]`，CI 可用基础安装。 |
| CLI | Typer | `0.26.8` | 实现 `agent-harness doctor/run/eval/policy/scaffold/approvals`。 |
| Durable execution | DBOS | `2.26.0` | service profile 默认 adapter；local profile 保留 SQLite-backed checkpoint。 |
| ORM | SQLAlchemy | `2.0.51` | 采用 2.0 typed declarative、async session、Repository + Unit of Work。 |
| Migration | Alembic | `1.18.5` | 统一 SQLite/PostgreSQL schema migration。 |
| PostgreSQL driver | asyncpg | `0.31.0` | service profile async driver；repository contract tests 以 async 路径为准。 |
| SQLite async bridge | aiosqlite | `0.22.1` | local profile 和 CI 使用 SQLite async adapter。 |
| Service database | PostgreSQL | `18.4` | 官网 2026-05-14 最新稳定补丁线；Docker Compose 可先固定 `postgres:18.4`。 |
| Queue / cache | Redis server | `7.2.4` for Docker Compose | Redis 8.8 已是当前 GA，但 Redis 8 许可证为 RSALv2/SSPLv1/AGPLv3 三选一；为 Apache-2.0 项目降低合规风险，P0 service profile 默认容器固定 Redis 7.2.4。 |
| Redis client | redis-py | `8.0.1` | 最新客户端支持 Redis 7.2 到 8.8；P0 只使用兼容 7.2 的基础能力。 |
| Observability 底座 | OpenTelemetry Python | `1.43.0` | OTel API/SDK 作为 provider adapter 前的统一协议。 |
| 推荐观测 provider | Logfire | `4.37.0` | 推荐 adapter；业务代码不直接 import。 |
| 可选观测 provider | Arize Phoenix | `17.18.0` | 可选 adapter contract，覆盖 trace/dataset/eval/feedback 工作流。 |
| 可选观测 provider | Langfuse Python SDK | `4.13.0` | v4 SDK；adapter 层处理 v4 API，不污染核心接口。 |
| MCP client SDK | mcp | `>=1.28.1,<2` | 官方 PyPI 说明 v1 是稳定线、v2 是 alpha；P0 明确 `<2` 防止破坏性升级。 |
| HTTP client | HTTPX | `0.28.1` | MCP HTTP/SSE、provider adapter 和 smoke tests 使用。 |
| Lint / Format | Ruff | `0.15.20` | `make quality` 的 lint + format 主工具。 |
| Typecheck | Pyright | `1.1.411` | 使用 Python wrapper 或 npm pyright；CI 固定版本，避免自动漂移。 |
| Test runner | pytest | `9.1.1` | unit、contract、integration、smoke、eval tests 的统一 runner。 |
| Async tests | pytest-asyncio | `1.4.0` | runtime、storage、API、event stream 的 async tests。 |
| Coverage | coverage.py | `7.15.0` | 产出 CI coverage artifact；和 pytest 分离配置。 |
| Git hooks | pre-commit | `4.6.0` | 本地 quality hook 和 license/header check 入口。 |
| Release automation | python-semantic-release | `10.6.0` | 统一 GitHub/GitLab 的 SemVer、tag、CHANGELOG、release artifact dry-run；不选 release-please 作为 P0 主线。 |
| 部署目标 | Docker Compose | Compose v2 | P0 只做本地 service profile，验证 PostgreSQL + Redis + API + worker 协作；不引入 Kubernetes。 |

## 技术栈验证来源

- Python 版本生命周期: https://devguide.python.org/versions/
- uv release: https://github.com/astral-sh/uv/releases
- Pydantic AI install/version: https://pydantic.dev/docs/ai/overview/install/ 和 https://pypi.org/project/pydantic-ai/
- DBOS Python docs/version: https://docs.dbos.dev/python/integrating-dbos 和 https://pypi.org/project/dbos/
- FastAPI / Uvicorn / Typer: https://pypi.org/project/fastapi/ , https://pypi.org/project/uvicorn/ , https://pypi.org/project/typer/
- SQLAlchemy / Alembic: https://pypi.org/project/SQLAlchemy/ , https://pypi.org/project/alembic/
- PostgreSQL releases: https://www.postgresql.org/
- Redis release/license/client: https://github.com/redis/redis/releases , https://hub.docker.com/_/redis , https://pypi.org/project/redis/
- OpenTelemetry / Logfire / Phoenix / Langfuse: https://pypi.org/project/opentelemetry-api/ , https://pypi.org/project/logfire/ , https://pypi.org/project/arize-phoenix/ , https://github.com/langfuse/langfuse-python
- MCP Python SDK: https://pypi.org/project/mcp/
- Quality / release tools: https://pypi.org/project/ruff/ , https://pypi.org/project/pyright/ , https://pypi.org/project/pytest/ , https://pypi.org/project/pytest-asyncio/ , https://pypi.org/project/coverage/ , https://pypi.org/project/pre-commit/ , https://pypi.org/project/python-semantic-release/

## 功能依赖图

```text
Phase 1 Monorepo / quality spine
  -> Phase 2 Core contracts / config / identity
    -> Phase 3 Storage / migrations / repositories
      -> Phase 4 Event / artifact / local telemetry spine
        -> Phase 5 Runtime / checkpoint / run lifecycle
          -> Phase 6 Agent registry / model / embedding adapters
            -> Phase 7 Auth / policy / HITL approval
              -> Phase 8 Tool system / file / shell / MCP
                -> Phase 9 Retrieval / RAG adapters
                  -> Phase 12 Service-app template / examples
        -> Phase 10 Observability provider adapters
          -> Phase 11 Eval Gate / trace-to-eval loop
            -> Phase 12 Service-app template / examples
              -> Phase 13 Service profile / split API-worker smoke
                -> Phase 14 Docs / ADR / maintainer guide
                  -> Phase 15 CI/CD / release automation / compliance
```

并行规则：Phase 2 之后，文档草稿可以和代码并行，但每个 Phase 的验收必须等代码、测试和文档证据一致后才算通过。

---

## Phase 1: Monorepo 骨架与质量门禁地基

**交付内容**：
- 搭建 `uv workspace` monorepo，使核心包、模板、示例、文档、脚本各有边界。
- 创建 `packages/agent-harness` 可打包 Python 包和 `templates/service-app` 可安装模板壳。
- 配置 `make quality`、`make test`、`make smoke-local` 的最小可运行命令，先让空骨架能被 CI 和本地工具检查。
- 添加 Apache-2.0 `LICENSE`、`NOTICE`、README 初稿和目录边界说明。

**关键文件**：
- `pyproject.toml` - workspace、tool config、根级 dependency groups。
- `uv.lock` - 锁定 P0 初始依赖解析结果。
- `packages/agent-harness/pyproject.toml` - 核心包 metadata、entry points、build backend。
- `packages/agent-harness/src/agent_harness/__init__.py` - 核心包公共版本入口。
- `templates/service-app/pyproject.toml` - 模板应用依赖 `agent-harness` 的 path/wheel 入口。
- `Makefile` - `quality`、`test`、`smoke-local`、`build`、`eval` 命令入口。
- `.pre-commit-config.yaml` - ruff、pyright、basic hygiene hook。
- `README.md` - 项目定位、目录树、边界规则、Quick Start 初稿。
- `LICENSE` - Apache-2.0 license。
- `NOTICE` - 第三方声明入口。

**验收标准**：
- 执行 `uv sync` 能解析 workspace。
- 执行 `uv build --package agent-harness` 能产出 wheel/sdist。
- 执行 `make quality` 能跑 ruff、pyright 和 import boundary 最小检查。
- 执行 `make smoke-local` 能在无真实模型 key、无外部 SaaS provider 下完成空模板健康检查。

---

## Phase 2: 核心契约、配置系统与身份上下文

**交付内容**：
- 定义 `agent_harness` 公共接口、错误模型、DTO 基础类、租户和身份上下文。
- 实现 `.env`、profile YAML、agent config YAML 的 typed settings 加载、合并、校验和错误提示。
- 定义 trust marker、source/ref、context input/output DTO 和 guardrail decision 基础契约，供后续输入护栏、ContextAssembler、MCP/retrieval output 复用。
- 建立厂商依赖隔离扫描，阻止业务 agent 和 app 入口直接 import Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 等实现。

**关键文件**：
- `packages/agent-harness/src/agent_harness/config/settings.py` - 根 settings loader。
- `packages/agent-harness/src/agent_harness/config/schemas.py` - profile、provider、storage、policy、agent config schema。
- `packages/agent-harness/src/agent_harness/identity/context.py` - `IdentityContext`、tenant/user/session model。
- `packages/agent-harness/src/agent_harness/contracts/dto.py` - Pydantic DTO 基类和 serialization 约束。
- `packages/agent-harness/src/agent_harness/contracts/errors.py` - 内部错误和 API error envelope 基础。
- `packages/agent-harness/src/agent_harness/contracts/trust.py` - `TrustLevel`、`SourceRef`、`ContextRef` 和 guardrail decision 基础类型。
- `packages/agent-harness/src/agent_harness/contracts/boundaries.py` - import boundary 声明和扫描规则。
- `templates/service-app/configs/profiles/local.yaml` - local profile 默认配置。
- `templates/service-app/configs/profiles/service.yaml` - service profile 默认配置。
- `templates/service-app/.env.example` - 开发者可复制配置样例。

**验收标准**：
- 缺失必填配置时启动失败，并输出字段路径和修复建议。
- local/service profile 都能解析到 typed config。
- trust marker、source/ref 和 context ref DTO 有 contract tests，并能序列化进事件 payload。
- 业务 agent 示例目录、`templates/service-app/app/*` 和 `examples/*` 的静态扫描不出现禁止的厂商 SDK import。
- `agent-harness doctor --profile local` 能显示配置加载状态。

**实现证据**：
- `openspec/changes/archive/2026-07-06-core-config-identity-contracts/` 保存了 Phase 2 的 proposal、四个 delta specs、design 和 tasks；`openspec/specs/` 已同步生成对应主规格。
- `agent_harness.contracts` 暴露 DTO、error envelope、trust/source/context refs、guardrail / policy decision 和 import boundary declarations。
- `agent_harness.identity` 暴露 `IdentityContext` 和 `PermissionContext`，local 默认 tenant/user/session 已通过 contract tests。
- `agent_harness.config` 暴露 typed profile / agent settings schemas 和 loader，支持 profile YAML、agent YAML、`.env` / environment overrides 和 structured diagnostics。
- `agent-harness doctor --profile local` 可报告 profile、storage、queue、observability、policy、identity 和 model 状态。
- `templates/service-app/configs/profiles/local.yaml` 和 `service.yaml` 已能通过 typed loader 校验；service profile 只声明 API/worker、storage/queue 和 provider-neutral 边界，不启动外部服务。
- 验证命令已通过：归档前 `openspec validate core-config-identity-contracts --type change --strict`，归档后 `openspec validate --all --strict`，以及实现阶段的 `make quality`、`make test`（18 passed）、`make smoke-local`、`make build`、`make license-check`、`uv run pre-commit run --all-files`、`uv run agent-harness doctor --profile local --profiles-dir templates/service-app/configs/profiles`。

---

## Phase 3: 存储、迁移与事务边界

**交付内容**：
- 实现 SQLAlchemy 2.0 async typed declarative model、Alembic migration 和 Repository + Unit of Work。
- 创建 tenant、session、run、checkpoint、trace、artifact、eval、policy、audit 的核心 schema 初版。
- 建立 SQLite local adapter 和 PostgreSQL service adapter 的 repository contract tests。

**关键文件**：
- `packages/agent-harness/src/agent_harness/storage/models.py` - ORM model 汇总或导出。
- `packages/agent-harness/src/agent_harness/storage/repositories.py` - repository interface。
- `packages/agent-harness/src/agent_harness/storage/uow.py` - Unit of Work。
- `packages/agent-harness/src/agent_harness/storage/adapters/sqlalchemy.py` - SQLAlchemy adapter。
- `packages/agent-harness/src/agent_harness/storage/migrations/env.py` - Alembic env。
- `packages/agent-harness/src/agent_harness/storage/migrations/versions/0001_core_schema.py` - P0 初始核心表。
- `packages/agent-harness/src/agent_harness/cli/doctor.py` - storage、migration、Redis、provider key、eval 目录检查。
- `templates/service-app/docker-compose.yml` - PostgreSQL 和 Redis service profile 依赖。

**验收标准**：
- local profile 执行 migration 后能创建 SQLite schema。
- service profile 执行 migration 后能创建 PostgreSQL schema。
- repository contract tests 在 SQLite 和 PostgreSQL 上行为一致。
- app/API/agent/eval 代码不能直接持有 SQLAlchemy session，只能走 repository 或 Unit of Work。

**实现状态**：
- 已实现 `agent_harness.storage` SQLAlchemy async adapter、Alembic `0001_core_schema`、Repository/UoW、local SQLite 和 service PostgreSQL migration。
- 已通过 `tests/contracts/test_storage_migration_uow_contracts.py`；service profile 通过 `make smoke-service`，PostgreSQL 镜像 `postgres:18`，Redis 本机 smoke 复用 `redis:8`，migration revision 为 `0001_core_schema`。

---

## Phase 4: CanonicalEvent、Artifact 与本地观测脊柱

**交付内容**：
- 定义 `CanonicalEvent` envelope、固定 P0 event types、terminal event 和 `seq` 规则。
- 纳入 `input.guardrail.*` 与 `context.assembly.*` 事件类型，记录来源、可信级别、截断和阻断摘要。
- 实现 local/jsonl event sink、artifact store、payload/payload_ref 策略和 secret redaction 基础。
- 实现 OTel mapping 的最小 facade，使后续 provider adapter 只做转换，不改变内部事件模型。

**关键文件**：
- `packages/agent-harness/src/agent_harness/events/types.py` - `CanonicalEvent` 和 event type 枚举。
- `packages/agent-harness/src/agent_harness/events/bus.py` - `EventBus`、`EventSink` interface。
- `packages/agent-harness/src/agent_harness/events/sinks/local_jsonl.py` - local/jsonl sink。
- `packages/agent-harness/src/agent_harness/artifacts/store.py` - artifact/ref/checksum 管理。
- `packages/agent-harness/src/agent_harness/security/redaction.py` - secret redaction 基础规则。
- `packages/agent-harness/src/agent_harness/security/guardrails.py` - input/output guardrail event payload 与阻断摘要。
- `packages/agent-harness/src/agent_harness/observability/otel.py` - CanonicalEvent 到 OTel span/metric/event 映射。
- `templates/service-app/app/api/sse.py` - SSE adapter 初版。

**验收标准**：
- 一个模拟 run 的 event stream 只能出现一个 terminal event。
- 同一 `run_id` 内事件 `seq` 单调递增，断线后可按 `seq` 继续读取。
- 大 payload 写入 artifact，事件正文只保留 `payload_ref`。
- guardrail/context assembly 事件只写摘要、source_ref、trust_level 和 truncation metadata，不写完整大 payload 或 secret。
- 未配置外部观测 provider 时 local/jsonl 仍产出 trace/eval/audit 证据。

**实现状态**：
- 已实现 `CanonicalEvent`、`EventBus`、local jsonl sink、filesystem artifact store、secret redaction、guardrail payload helper、OTel mapping facade 和 SSE formatter。
- 已通过 `tests/contracts/test_canonical_events_artifacts_contracts.py`，覆盖完整 P0 event catalog、terminal uniqueness、seq resume、payload_ref、redaction、OTel mapping、reasoning 默认隐藏和 SSE JSON。

---

## Phase 5: Durable Runtime、Checkpoint 与 Run 生命周期

**交付内容**：
- 实现 `RunOrchestrator`、run state machine、checkpoint、resume token 和 idempotency key。
- 接入 local SQLite-backed checkpoint，并为 service profile 建立 DBOS adapter 边界。
- 打通 CLI/API 的 run 创建、取消、恢复、事件流读取的最小闭环。

**关键文件**：
- `packages/agent-harness/src/agent_harness/runtime/orchestrator.py` - run 编排入口。
- `packages/agent-harness/src/agent_harness/runtime/state.py` - run status、terminal status、state transition。
- `packages/agent-harness/src/agent_harness/runtime/checkpoints.py` - `CheckpointStore` interface。
- `packages/agent-harness/src/agent_harness/runtime/idempotency.py` - duplicate submission 防护。
- `packages/agent-harness/src/agent_harness/adapters/runtime/dbos.py` - DBOS adapter 边界。
- `templates/service-app/app/api/routes/runs.py` - run API routes。
- `templates/service-app/app/cli/run.py` - `agent-harness run <agent_id>` CLI。
- `templates/service-app/app/workers/runtime_worker.py` - runtime worker 壳。

**验收标准**：
- 使用 fake agent 创建 run 后，API 和 CLI 都能得到 terminal event。
- 同一 idempotency key 重复提交不会产生重复 run。
- run 触发 checkpoint 后重启进程仍可 resume。
- service profile 代码只依赖 `DBOSRuntimeAdapter` interface，不把 DBOS API 泄漏给业务 agent。

**实现状态**：
- 已实现 `RunOrchestrator`、run state、idempotency、checkpoint/resume、`agent-harness run` CLI、template FastAPI app factory、run create/detail/events/cancel/resume routes、runtime worker shell 和 `DBOSRuntimeAdapter` boundary。
- 已通过 `tests/contracts/test_runtime_checkpoint_runs_contracts.py`，覆盖 runtime public DTO/Protocol seam、fake run terminal event、非法 terminal transition、idempotency、checkpoint 后重建 orchestrator resume、resume token/run_id 归属校验、CLI run、API request/error envelope、FastAPI OpenAPI route registration、event stream seam 和 worker run seam。

---

## Phase 6: Agent Registry、模型路由与 Embedding

**交付内容**：
- 实现多 agent registry、`AgentDescriptor`、agent config schema 校验和受控 delegation 配置读取。
- 实现 Pydantic AI 默认 adapter、FakeModelProvider、ModelRouter、预算估算、timeout、fallback。
- 实现 ContextAssembler：收口 history、retrieval、tool output、artifact refs、trust marker、token budget 和上下文降级链。
- 实现 EmbeddingProvider、mock/local embedding、OpenAI-compatible embedding adapter 和 embedding cache。

**关键文件**：
- `packages/agent-harness/src/agent_harness/registry/descriptor.py` - `AgentDescriptor`。
- `packages/agent-harness/src/agent_harness/registry/registry.py` - 多 agent 加载、校验、查询。
- `packages/agent-harness/src/agent_harness/models/router.py` - model routing、fallback、budget check。
- `packages/agent-harness/src/agent_harness/models/providers.py` - model provider interface。
- `packages/agent-harness/src/agent_harness/context/assembler.py` - ContextAssembler、history trimming、retrieval/tool output injection。
- `packages/agent-harness/src/agent_harness/context/budget.py` - token budget、context truncation、fallback decision summary。
- `packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py` - Pydantic AI adapter。
- `packages/agent-harness/src/agent_harness/adapters/models/fake.py` - fake model provider。
- `packages/agent-harness/src/agent_harness/embeddings/provider.py` - embedding provider interface。
- `packages/agent-harness/src/agent_harness/embeddings/cache.py` - embedding cache。
- `templates/service-app/app/api/routes/agents.py` - `/api/v1/agents`。
- `templates/service-app/agents/examples/basic/config.yaml` - registry smoke agent。

**验收标准**：
- `agent-harness agents list` 能列出已配置 agent。
- 重复 `agent_id` 或不合法 config 会被 registry 拒绝。
- fake model 下不需要真实 API key 就能跑测试和 eval smoke。
- ContextAssembler 生成 context assembly trace，能解释 source、trust_level、token budget、truncation 和 fallback decision。
- 业务 agent 不直接 import `pydantic_ai`；替换 fake adapter 后 contract tests 仍通过。
- `API-Contract.md` 的 `AGT-001` 已扩展为完整 endpoint 条目，OpenAPI drift test 覆盖 `/api/v1/agents` 的 route、schema、错误 envelope 和 registry validation error。

---

## Phase 7: 认证、PolicyEngine 与 HITL 审批

**交付内容**：
- 实现 API Key / Bearer Token 认证，注入 `IdentityContext`，未启用多租户时使用 default tenant/user。
- 实现 `PolicyEngine`、YAML provider、DB provider、默认危险动作策略和 audit log。
- 实现 approval required、approve/deny、checkpoint resume 的 HTTP/CLI 闭环。

**关键文件**：
- `packages/agent-harness/src/agent_harness/auth/api_key.py` - API key / bearer 认证。
- `packages/agent-harness/src/agent_harness/policy/engine.py` - policy decision 核心。
- `packages/agent-harness/src/agent_harness/policy/providers.py` - YAML 和 DB provider interface。
- `packages/agent-harness/src/agent_harness/policy/defaults.py` - 默认 require_approval 清单。
- `packages/agent-harness/src/agent_harness/approvals/service.py` - approval create/resolve/resume。
- `packages/agent-harness/src/agent_harness/audit/service.py` - audit log 写入。
- `templates/service-app/app/api/dependencies/auth.py` - API 认证依赖。
- `templates/service-app/app/api/routes/approvals.py` - approval API routes。
- `templates/service-app/app/cli/approvals.py` - approval CLI。
- `templates/service-app/configs/policy/default.yaml` - 默认策略配置。

**验收标准**：
- 无效 Bearer Token 调用 P0 API 返回认证错误且不创建 run。
- 未配置多租户时 run/session/trace/eval 均带 `tenant_id="default"`。
- shell、删除文件、workspace 外访问、写 approved dataset、修改 policy 等动作默认产生 `approval.required` 或被拒绝。
- approve 后 run 从 checkpoint resume，deny 后 run 按策略失败或 fallback，audit log 记录审批人、动作、结果和 trace。
- `API-Contract.md` 中 auth、policy、approval 相关 endpoint 已扩展为完整条目，局部 OpenAPI drift test 覆盖 401/403、`ApiErrorEnvelope`、approval 状态冲突和 request_id。

---

## Phase 8: ToolRegistry、FileTool、ShellTool 与 MCP Client

**交付内容**：
- 实现 `ToolRegistry`，统一本地工具、MCP 工具、schema validation、policy interception、trace/audit。
- 实现 Workspace FileTool：read/write/list/search/patch/delete，受 workspace 根目录、`.agentignore` 和 policy 控制。
- 实现 ShellTool 默认 disabled、显式启用、allowlist/denylist、timeout、stdout/stderr 截断、artifact_ref。
- 实现 MCP client connector：stdio、HTTP/SSE、tool discovery、allowlist、policy、untrusted output 标注和 trace/audit。

**关键文件**：
- `packages/agent-harness/src/agent_harness/tools/registry.py` - tool registry。
- `packages/agent-harness/src/agent_harness/tools/schema.py` - tool input/output schema validation。
- `packages/agent-harness/src/agent_harness/tools/output_guard.py` - tool/MCP output source_ref、trust_level、截断和注入检测。
- `packages/agent-harness/src/agent_harness/tools/file_tool.py` - workspace file operations。
- `packages/agent-harness/src/agent_harness/tools/shell_tool.py` - guarded shell execution。
- `packages/agent-harness/src/agent_harness/tools/workspace.py` - workspace root、`.agentignore`、path guard。
- `packages/agent-harness/src/agent_harness/mcp/client.py` - MCP client interface。
- `packages/agent-harness/src/agent_harness/adapters/mcp/python_sdk.py` - official MCP SDK adapter。
- `templates/service-app/configs/tools.yaml` - tool allowlist / denylist / MCP server config。

**验收标准**：
- workspace 外路径默认被拒绝或要求审批。
- shell tool 默认 disabled；显式启用后仍受 allowlist、timeout、环境变量白名单和 approval 控制。
- MCP tool 未在 allowlist 时被 policy 拒绝。
- 大 tool output 被截断并写 artifact_ref，事件和 audit 不塞入完整大文本。
- MCP/tool output 进入 ContextAssembler 前必须带 source_ref、trust_level 和 truncation metadata；指令型文本不得覆盖 system/policy/developer 指令。

---

## Phase 9: RetrievalProvider 与 RAG 能力

**交付内容**：
- 实现 `RetrievalProvider` interface、local SQLite FTS5/BM25 adapter 和 PostgreSQL retrieval adapter。
- 实现 optional PGroonga、optional pgvector adapter 探测、doctor 降级提示和 hybrid retrieval + RRF interface。
- 提供 RAG assistant 示例所需的 indexing、query、citation、untrusted chunk 标注和 retrieval eval 基础。

**关键文件**：
- `packages/agent-harness/src/agent_harness/retrieval/provider.py` - retrieval provider interface。
- `packages/agent-harness/src/agent_harness/retrieval/local_bm25.py` - SQLite FTS5/BM25 local adapter。
- `packages/agent-harness/src/agent_harness/retrieval/postgres.py` - PostgreSQL retrieval adapter。
- `packages/agent-harness/src/agent_harness/retrieval/pgroonga.py` - optional PGroonga adapter。
- `packages/agent-harness/src/agent_harness/retrieval/pgvector.py` - optional pgvector adapter。
- `packages/agent-harness/src/agent_harness/retrieval/hybrid.py` - RRF merge interface。
- `packages/agent-harness/src/agent_harness/retrieval/context.py` - retrieval chunk source_ref、citation、trust_level 和 context injection DTO。
- `templates/service-app/agents/examples/rag_assistant/config.yaml` - RAG 示例 config。
- `templates/service-app/agents/examples/rag_assistant/evals/approved.yaml` - RAG eval 基础数据。

**验收标准**：
- local profile 不依赖 PostgreSQL 扩展也能返回 BM25 retrieval 结果。
- service profile 中 PGroonga 或 pgvector 未安装时 `agent-harness doctor` 输出降级提示，系统不崩溃。
- hybrid retrieval adapter 可合并 BM25/vector 结果并输出可追踪 ranking。
- RAG 示例回答带 citation 或明确说明未找到出处。
- 检索 chunk 注入上下文前保留 citation/source_ref/trust_level；prompt injection 文本只能作为引用内容，不能覆盖系统策略。

---

## Phase 10: Observability Provider Adapters 与脱敏

**交付内容**：
- 扩展 `TelemetryFacade`，实现 local/jsonl 永久保留、OTel exporter、Logfire adapter、Phoenix adapter、Langfuse adapter 的 contract 层。
- 对 runtime、tool、model、retrieval、eval、approval、audit 事件统一加 trace/span/tenant/user/agent/run/session 关联字段。
- 强化 secret redaction，确保 secret 不进入 trace、eval、audit、local/jsonl、错误栈和外部 provider。

**关键文件**：
- `packages/agent-harness/src/agent_harness/observability/facade.py` - telemetry facade。
- `packages/agent-harness/src/agent_harness/observability/context.py` - trace/span context propagation。
- `packages/agent-harness/src/agent_harness/adapters/observability/logfire.py` - Logfire adapter。
- `packages/agent-harness/src/agent_harness/adapters/observability/phoenix.py` - Phoenix adapter。
- `packages/agent-harness/src/agent_harness/adapters/observability/langfuse.py` - Langfuse adapter。
- `packages/agent-harness/src/agent_harness/observability/redaction.py` - provider 前脱敏规则。
- `templates/service-app/configs/profiles/local.yaml` - local/jsonl 默认配置。
- `templates/service-app/configs/profiles/service.yaml` - OTel/provider 配置入口。

**验收标准**：
- 未配置任何 SaaS provider 时 local/jsonl 仍产出完整本地证据。
- 配置 Logfire/Phoenix/Langfuse adapter 时，adapter contract tests 通过且业务 agent 无 provider SDK import。
- 外部 provider 失败不丢本地 trace 和 audit。
- secret fixture 在 trace、eval、audit、local/jsonl 和 provider payload 中均被脱敏或被阻止写入。

---

## Phase 11: Eval Gate 与 Trace 到 Eval 闭环

**交付内容**：
- 实现 `EvalCaseFactory`、failed/low-score detector、review queue、draft/approved dataset 分离和人工审核流程。
- 实现 `EvalRunner`、approved dataset 执行、ScoreSink、本地 JSONL score 和 provider score 写回。
- 接入 CLI/API：draft、approve、list、run eval、查看 score。

**关键文件**：
- `packages/agent-harness/src/agent_harness/evals/cases.py` - eval case model、draft/approved 状态。
- `packages/agent-harness/src/agent_harness/evals/factory.py` - trace 到 draft case。
- `packages/agent-harness/src/agent_harness/evals/review_queue.py` - human review queue。
- `packages/agent-harness/src/agent_harness/evals/runner.py` - eval runner。
- `packages/agent-harness/src/agent_harness/evals/score_sink.py` - score sink interface。
- `packages/agent-harness/src/agent_harness/adapters/evals/local_jsonl.py` - local eval result sink。
- `templates/service-app/app/api/routes/evals.py` - eval API routes。
- `templates/service-app/app/cli/eval.py` - eval CLI。
- `templates/service-app/eval-cases/drafts/.gitkeep` - draft dataset 目录。
- `templates/service-app/eval-cases/approved/.gitkeep` - approved dataset 目录。

**验收标准**：
- failed run trace 执行 `agent-harness eval draft` 后生成 draft case。
- 人工 approve 后 case 进入 approved dataset 并写 audit log；默认不允许自动写 approved dataset。
- `make eval` 只跑 approved cases，输出 eval result 和 score sink 记录。
- score 可写回 local/jsonl，并可通过 Logfire/Phoenix/Langfuse adapter contract 写入 provider。
- `API-Contract.md` 中 eval draft、approved dataset 和 eval run endpoint 已扩展为完整条目，局部 OpenAPI drift test 覆盖人工确认、secret 脱敏错误和 score sink 降级语义。

---

## Phase 12: Service App 模板与四个 P0 示例 Agent

**交付内容**：
- 完成 `templates/service-app` 的 FastAPI、CLI、worker、configs、tests、docs、docker-compose 和 README。
- 实现四个薄样例 agent：RAG assistant、ticket triage、repo analyst、dev assistant，分别验证 retrieval、结构化输出、file tool、shell/HITL。
- 完成 `/api/v1/...` P0 endpoint、OpenAPI schema、Swagger/Redoc 管理面和 CLI 命令集。

**关键文件**：
- `templates/service-app/app/main.py` - FastAPI app。
- `templates/service-app/app/api/router.py` - `/api/v1` router。
- `templates/service-app/app/api/routes/health.py` - health route。
- `templates/service-app/app/api/routes/agents.py` - agents routes。
- `templates/service-app/app/api/routes/runs.py` - run routes。
- `templates/service-app/app/api/routes/policies.py` - policy check route。
- `templates/service-app/app/cli/main.py` - Typer root CLI。
- `templates/service-app/app/workers/runtime_worker.py` - worker entry。
- `templates/service-app/agents/examples/rag_assistant/agent.py` - RAG assistant 示例。
- `templates/service-app/agents/examples/ticket_triage/agent.py` - ticket triage 示例。
- `templates/service-app/agents/examples/repo_analyst/agent.py` - repo analyst 示例。
- `templates/service-app/agents/examples/dev_assistant/agent.py` - dev assistant 示例。
- `templates/service-app/README.md` - app developer 快速开始和模板边界。

**验收标准**：
- `agent-harness agents list` 能列出四个 P0 示例。
- local profile 下 `make dev` 或 `agent-harness run <agent_id>` 至少一种入口可运行示例 agent。
- OpenAPI schema 包含 Spec 列出的 P0 endpoints。
- OpenAPI schema 与 `API-Contract.md` 中所有 P0 endpoint、schema、错误 envelope 和 request_id 规则完成全量漂移复扫。
- 四个示例 fake model eval 均能确定性通过，且示例不直接 import 厂商 SDK。

---

## Phase 13: Service Profile、API/Worker 分进程与未来拆分边界

**交付内容**：
- 完成 Docker Compose service profile，PostgreSQL、Redis、API 进程和 runtime worker 使用同一 storage/queue 配置协作。
- 验证 DBOS service adapter、shared checkpoint、event stream 和 run worker pickup。
- 在代码和文档中固定未来微服务拆分顺序：先拆 worker，再拆 tool/model gateway，最后拆 observability/event pipeline；storage service 仅在 repository contract 稳定后拆；guardrail/context assembly 边界必须随 API/worker/model/tool gateway 保持 DTO/CanonicalEvent 兼容。

**关键文件**：
- `templates/service-app/docker-compose.yml` - PostgreSQL、Redis、API、worker。
- `templates/service-app/Makefile` - `smoke-service`、`migrate-service`、`worker`。
- `templates/service-app/app/workers/runtime_worker.py` - service worker 主循环。
- `packages/agent-harness/src/agent_harness/runtime/queue.py` - run queue interface。
- `packages/agent-harness/src/agent_harness/adapters/queue/redis.py` - Redis queue adapter。
- `packages/agent-harness/src/agent_harness/adapters/runtime/dbos.py` - DBOS workflow/checkpoint integration。
- `docs/architecture.md` - 当前同进程形态和未来拆分边界。
- `docs/adr/0001-p0-service-boundaries.md` - P0 不强制微服务但预留接口的决策。

**验收标准**：
- `make smoke-service` 能启动 PostgreSQL、Redis、API 和 worker。
- 分别启动 API 进程和 worker 进程后提交 run，run 被 worker 执行并产出 event stream。
- API、worker、tool/model adapter 交换数据只使用 Pydantic DTO、CanonicalEvent、repository/provider/facade interface。
- API/worker/model/tool gateway 拆分后仍保留 source_ref、trust_level、context assembly trace 和 guardrail/audit 关联字段。
- 文档能让维护者指出 API、runtime worker、model/tool gateway、storage、event pipeline 的当前形态和未来拆分路径。

---

## Phase 14: 深度文档、ADR 与维护者指南

**交付内容**：
- 完成面向 app developer 和 scaffold maintainer 的 README、深度文档和 ADR。
- 写清 adapter contract、extension guide、security policy、guardrail / context assembly / trust boundary、eval-observability loop、release process 和目录禁止跨边界规则。
- 为每个能力块补充可执行命令、验收证据位置和常见故障排查。

**关键文件**：
- `README.md` - 根 README 最终版。
- `docs/architecture.md` - 架构和未来拆分边界。
- `docs/extension-guide.md` - 扩展 agent、tool、model、retrieval、observability、eval adapter。
- `docs/adapter-contracts.md` - provider/repository/facade contract。
- `docs/context-and-trust-boundary.md` - Agent Loop、HITL 回边、SSE/WS 回传、ContextAssembler 和 untrusted input 处理。
- `docs/eval-observability-loop.md` - trace -> eval -> score -> provider 闭环。
- `docs/security-policy.md` - auth、policy、approval、workspace、secret redaction。
- `docs/release-process.md` - SemVer、tag、CHANGELOG、private publish、artifact。
- `docs/adr/0002-vendor-adapter-isolation.md` - 上游隔离决策。
- `docs/adr/0003-redis-7-2-for-p0-license-risk.md` - Redis 版本和 license 风险决策。

**验收标准**：
- 新开发者阅读 README 后能运行 local profile、理解目录职责和禁止跨边界规则。
- 维护者阅读 docs 后能找到 adapter contract、release process、安全策略、context/trust boundary、ADR 和 eval/observability 闭环。
- 所有文档命令都能在当前 repo 执行或明确标注需要 service profile。
- 文档中的技术栈版本和 `pyproject.toml` / `uv.lock` 保持一致。

---

## Phase 15: CI/CD、Release Automation 与合规收口

**交付内容**：
- 建立 GitHub Actions 和 GitLab CI 等价质量门禁：install、ruff、pyright、unit/contract tests、integration、eval、smoke-local、smoke-service、build、license check、release dry-run。
- 实现 python-semantic-release dry-run、版本计算、tag 名称、CHANGELOG preview、release notes、wheel/sdist artifact、私有 registry 发布路径。
- 完成 license check、NOTICE 追踪、CI artifacts 归档和 P0 acceptance matrix 最终证据。

**关键文件**：
- `.github/workflows/ci.yml` - GitHub CI。
- `.github/workflows/release.yml` - GitHub release dry-run / publish path。
- `.gitlab-ci.yml` - GitLab 等价 pipeline。
- `scripts/license_check.py` - license / NOTICE / vendoring 检查。
- `scripts/import_boundary_check.py` - import boundary CI 检查。
- `scripts/release_dry_run.py` - release preview wrapper。
- `CHANGELOG.md` - generated changelog 输出。
- `docs/release-process.md` - release 操作文档。
- `docs/p0-acceptance-matrix.md` - P0 验收矩阵和证据链接。

**验收标准**：
- GitHub CI 和 GitLab CI 都跑等价命令集并产出 test report、coverage、trace sample、eval result、smoke logs、wheel/sdist、release preview artifact。
- 有 releasable commits 时 release dry-run 能生成下一版本、tag 名称、CHANGELOG 预览和 wheel/sdist artifact。
- 无 releasable commits 时 release dry-run 不创建 tag 或 release。
- `LICENSE` 为 Apache-2.0，`NOTICE` 可追踪第三方声明，license check 能阻止未声明 vendoring 或不兼容 license。

---

## 数据库表

| 表名 | 所属 Phase | 用途 |
|------|-----------|------|
| `tenants` | Phase 3 | 默认租户和未来多租户隔离基础。 |
| `identities` | Phase 7 | API key / bearer token 解析后的身份记录或本地默认身份。 |
| `sessions` | Phase 3 | 用户会话和 agent session 关联。 |
| `agent_runs` | Phase 3 | run 生命周期、状态、parent run、idempotency。 |
| `checkpoints` | Phase 3 | durable runtime checkpoint 和 resume token。 |
| `canonical_events` | Phase 4 | run event stream、seq、terminal event、visibility。 |
| `trace_refs` | Phase 4 | local/provider trace 引用。 |
| `artifacts` | Phase 4 | 大 payload、tool output、eval evidence、checksum。 |
| `embedding_cache` | Phase 6 | embedding 输入 hash、provider、vector ref、cache metadata。 |
| `api_keys` | Phase 7 | API Key / Bearer Token 本地认证材料的 hash 和权限范围。 |
| `policy_rules` | Phase 7 | YAML/DB policy provider 的规则落库。 |
| `approvals` | Phase 7 | HITL approval required / approve / deny 记录。 |
| `audit_logs` | Phase 7 | policy decision、approval、tool、eval dataset 写入审计。 |
| `guardrail_checks` | Phase 7 | input/tool/retrieval guardrail 检查摘要、decision、source_ref 和 artifact_ref；Phase 4 先以 CanonicalEvent/local evidence 表达。 |
| `context_assemblies` | Phase 6 | context input refs、token budget、trust summary、truncation summary 和 output_ref。 |
| `workspaces` | Phase 8 | per-run 或 per-agent workspace 根路径和 policy 引用。 |
| `tool_invocations` | Phase 8 | tool name、args_ref、result_ref、status、duration。 |
| `retrieval_documents` | Phase 9 | RAG 示例和 local/service retrieval 的文档 metadata。 |
| `retrieval_chunks` | Phase 9 | chunk 文本 ref、BM25/vector metadata、citation ref。 |
| `eval_cases` | Phase 11 | draft / approved eval case，关联 trace 和 agent。 |
| `eval_runs` | Phase 11 | 一次 eval 执行的状态和 score summary。 |
| `eval_scores` | Phase 11 | per-case / per-metric score 和 provider ref。 |
| `release_records` | Phase 15 | version、tag、CHANGELOG、artifact、commit sha。 |

## Spec 覆盖矩阵

| Product-Spec 条目 | 覆盖 Phase |
|---|---|
| REQ-001 Monorepo / uv workspace | Phase 1 |
| REQ-002 核心包与上游隔离 | Phase 2, Phase 6, Phase 10 |
| REQ-003 后端服务型模板 | Phase 1, Phase 12 |
| REQ-004 配置系统 | Phase 2 |
| REQ-005 存储、迁移与事务边界 | Phase 3 |
| REQ-006 Durable runtime、checkpoint、resume | Phase 5, Phase 13 |
| REQ-007 多 agent registry 与 delegation | Phase 6 |
| REQ-008 API、CLI 与管理面 | Phase 5, Phase 7, Phase 11, Phase 12 |
| REQ-009 租户、身份与认证 | Phase 2, Phase 7 |
| REQ-010 PolicyEngine、权限拦截、InputGuardrail 与 HITL | Phase 2, Phase 4, Phase 7 |
| REQ-011 工具系统、Shell、File、MCP | Phase 8 |
| REQ-012 模型、预算、上下文组装与 embedding | Phase 2, Phase 4, Phase 6 |
| REQ-013 Retrieval 与 RAG | Phase 6, Phase 9, Phase 12 |
| REQ-014 CanonicalEvent 与流式输出 | Phase 4, Phase 5 |
| REQ-015 Observability 转换层 | Phase 4, Phase 10 |
| REQ-016 Eval Gate 与 trace/eval 闭环 | Phase 10, Phase 11 |
| REQ-017 示例 agent | Phase 12 |
| REQ-018 README 与文档体系 | Phase 1, Phase 14 |
| REQ-019 TDD、测试与质量门禁 | Phase 1, all phases |
| REQ-020 CI/CD 与 Release Automation | Phase 15 |
| REQ-021 开源合规与许可证 | Phase 1, Phase 14, Phase 15 |
| REQ-022 部署边界与未来微服务拆分基础 | Phase 2, Phase 4, Phase 5, Phase 13, Phase 14 |

## 开发规则

- 包管理器只用 `uv`；不使用 poetry、pipenv、npm 作为 Python 依赖主流程。
- 每个 Phase 必须先有失败测试或 contract test，再实现代码；不接受先堆代码后补测试作为 Phase 完成方式。
- 新增或修改 HTTP endpoint 必须先更新 `API-Contract.md`，再新增局部 OpenAPI drift contract test，最后实现 route；发布前全量复扫只做证据汇总，不作为第一次发现契约问题的入口。
- 每完成一个 Phase 执行四步走：Code Review -> 测试完整性 -> 编译验证 -> 功能测试。
- 四步走全部通过后才能 commit；commit message 用 `feat`、`fix`、`refactor`、`chore` 前缀。
- `packages/agent-harness` 不依赖 `templates/*` 或 `examples/*`；模板只能通过 path dependency 或 wheel 使用核心包。
- 业务 agent、模板 app、eval runner 不直接 import Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 或直接操作 SQLAlchemy session。
- 所有跨边界数据必须使用 Pydantic DTO、CanonicalEvent、repository interface、provider interface 或 facade。
- local/jsonl 永远可用；外部 provider 失败不得导致本地证据丢失。
- 危险动作默认走 policy 和 approval；不得为了测试方便绕过 PolicyEngine。
- 外部输入、MCP output、tool output、retrieval chunk 默认按 untrusted 处理；进入模型上下文必须经过 ContextAssembler 并保留 source_ref、trust_level 和 truncation metadata。
- `eval-cases/approved` 只能由审核流程写入；自动 detector 只能写 draft。
- 所有核心数据、trace、eval、audit、artifact 必须带 `tenant_id`，run 相关数据必须带 `agent_id`、`run_id` 或 `trace_id`。

## 已知风险与限制

| 风险 | 影响范围 | 处理 Phase | 当前状态 | 处理方式 / 验收信号 |
|------|----------|------------|----------|----------------------|
| Pydantic AI 2.5.0 刚发布，上游 API 和包边界可能变化。 | 核心 runtime、registry、model adapter 和业务 agent import 边界。 | Phase 2、Phase 6、Phase 10 | 部分缓解 | Phase 2 已定义 `agent_harness` 公共契约和 vendor import 边界；后续 Phase 6/10 再实现具体 adapter。 |
| DBOS 2.26.0 是关键 service runtime 依赖，过早耦合会污染领域模型。 | Durable runtime、checkpoint、worker lifecycle。 | Phase 5、Phase 13 | 未处理 | 通过 `DBOSRuntimeAdapter` 隔离；验收时证明内部 run/checkpoint model 不依赖 DBOS 类型。 |
| Redis 8.8 许可证变化影响 Apache-2.0 合规判断。 | Docker Compose service profile、queue/cache adapter、发布合规。 | Phase 13、Phase 15 | 已缓解 | P0 Docker Compose 固定 Redis 7.2.4；后续升级必须走 ADR 和 license review。 |
| PGroonga 和 pgvector 是 optional adapter，可能拖累 local profile 或 CI。 | Retrieval、embedding cache、service profile smoke。 | Phase 9、Phase 13 | 未处理 | local profile 不硬依赖 PGroonga/pgvector；service profile 单独验 PostgreSQL 扩展和 adapter 行为。 |
| P0 只做可拆边界，不做完整微服务；如果 API/worker/storage/tool 边界不清，后续会重构。 | API、runtime worker、model/tool gateway、storage、event/observability。 | Phase 2、Phase 4、Phase 5、Phase 13、Phase 14 | 部分缓解 | Phase 2 已通过 typed service profile、DTO/context/identity contracts 和 README 部署边界说明建立接口基础；Phase 13 做 API/worker 分进程 smoke。 |
| Phoenix、Langfuse、Logfire 的 dataset/score/workflow 能力差异大。 | Observability adapter、Eval Gate、score sink。 | Phase 10、Phase 11 | 未处理 | P0 先做 provider-neutral contract 和 local/jsonl fallback；复杂 provider-native workflow 放 P1。 |
| Prompt injection / tool output injection 如果后补，会污染所有 agent 和 eval 证据。 | Access input、MCP、tools、retrieval、context assembly、audit。 | Phase 2、Phase 4、Phase 6、Phase 8、Phase 9 | 部分缓解 | Phase 2 已定义 trust marker/source_ref/context ref 和 guardrail decision DTO；后续在 guardrail、ContextAssembler、tool/MCP/retrieval adapters 中强制传播并写入 trace/audit。 |
