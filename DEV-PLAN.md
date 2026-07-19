# Development Plan - Agent Harness Layer

> 本文件记录 Agent Harness Layer 的开发阶段划分、当前进度和剩余工作。
> 新 session 启动时应首先阅读 `Product-Spec.md`、本文件和最新 git 状态，再继续开发。

---

## 当前状态

- Product Spec: `Product-Spec.md` 已存在，当前变更记录版本为 2026-07-20 的 v1.11。
- Design Brief: 未提供。P0 不做产品化前端 UI，本计划按后端脚手架、架构图和既有 Spec 降级规划。
- 设计稿 / 架构图: 已读取 `docs/architecture/pydantic-ai-agent-architecture.drawio`，按 5 层运行中轴、Agent Loop / HITL / 流式回边、Eval Gate、Observability、信任边界和未来拆分边界组织开发顺序。
- API Contract: `API-Contract.md` 已补入。由于 P0 不做产品化前端 UI，契约按入口 / 调用方映射 CLI、OpenAPI 调用方、service-app、worker 和未来 Access/API gateway；新增或修改 HTTP endpoint 前必须先更新契约，再做局部 OpenAPI 漂移检查。
- OpenSpec: 仓库存在 `openspec/`；Phase 1-14 的已完成 changes 均已归档并同步主规格，当前无 active change。
- 代码状态: Phase 1-14 已完成计划内实现或文档交付、验证、fresh Stage 1/2 审查与归档；Phase 15 未开始。
- 计划模式: 迭代模式。已完成 Phase 保持冻结，只更新状态、剩余工作、风险和后续 Phase 入口。

## 当前进度

| 项目 | 状态 | 证据 / 下一步 |
|------|------|---------------|
| 总体状态 | Phase 1-14 已完成并归档 | `maintainer-deep-documentation` 已同步主规格并归档；Phase 15 未开始。 |
| 当前 Phase | Phase 15 未开始 | 是否进入 CI/CD、Release Automation 与合规收口由用户另行决定。 |
| 已完成 Phase | Phase 1, Phase 2, Phase 3, Phase 4, Phase 5, Phase 6, Phase 7, Phase 8, Phase 9, Phase 10, Phase 11, Phase 12, Phase 12.5, Phase 13, Phase 14 | Phase 13 与 Phase 14 的 changes 均已同步主规格并归档。 |
| 当前 OpenSpec change | 无 | `openspec list --json` 返回空 active changes。 |
| 当前验证基线 | Phase 14 完整门禁通过 | quality 通过；全量 `1001 passed, 222 skipped`；eval `11/11`、local smoke、复制模板的 fingerprint/migration/smoke/run/dev-health Quick Start、真实 PostgreSQL/Redis/DBOS service smoke、build、license、链接/锚点/外链/版本、strict 与 diff check 均通过。 |
| 当前阻塞项 | 无 | Phase 14 归档差异仍需通过 fresh Stage 1/2 review 后才可写入 `clean`。 |
| 当前建议下一步 | 等待用户决定是否启动 Phase 15 | 不自动创建 Phase 15 change，不 commit、push、发布或部署。 |

## 剩余工作

### 立即下一步

- Phase 13.5-13.9 的已实现 changes 已同步主规格并归档。
- Phase 14 的交付、验证、主规格同步与归档已完成；归档路径为 `openspec/changes/archive/2026-07-19-maintainer-deep-documentation/`。
- Phase 15 仍保持未开始，不因 Phase 14 文档中的未来合同自动开工。

### 后续 Phase

- Phase 12: Service App 模板与四个 P0 示例 Agent，已完成并归档。
- Phase 12.5: Eval Experiment 与 Harness Hill-Climb 闭环，已实现、验证、提交并归档。
- Phase 13: Service Profile、API/Worker 分进程与未来拆分边界，已实现、验证、同步主规格并归档。
- Phase 13.5: Run OpenAPI response/status 准确性已实现、审查，并于 2026-07-19 归档。
- Phase 13.6: 配置启动失败与 Docker secret file 加载已实现、审查，并于 2026-07-19 归档。
- Phase 13.6A: Embedding cache tenant isolation、canonical run trace 与 approval/event 关联已实现、审查，并于 2026-07-19 归档；依赖 Phase 13.6。
- Phase 13.7: Model/Embedding usage evidence、durable settlement 与 local latency 已完成 17/17 tasks、审查，并于 2026-07-19 归档；依赖 Phase 13.6A。
- Phase 13.8: 真实受控 delegation 与 parent aggregation 已完成 14/14 tasks、联合审查，并于 2026-07-19 归档；依赖 Phase 13.7。
- Phase 13.8A: Parent execution-tree shared budget、typed fingerprint secret 与 `0016` 历史迁移修正已完成 27/27 tasks、联合审查，并于 2026-07-19 归档；依赖 Phase 13.8。
- Phase 13.9: SSE transport、Last-Event-ID、CLI stream 与首 frame 性能已实现并通过完整门禁及最终 3-review，并于 2026-07-19 归档。
- Phase 14: 深度文档、ADR 与维护者指南，已交付、验证、同步主规格并归档。
- Phase 15: CI/CD、Release Automation 与合规收口。

### 关键验收状态

- AC-017：RUN-001 至 RUN-006 已全部进入运行时 OpenAPI，并由 path/header/query/media type/status 双向精确合同覆盖；Phase 13.9 的最终 3-review 与归档均已收口。
- AC-008、AC-063：Phase 13.6 已实现并提交 application startup fail-closed、受控 Docker secret file 加载和公开 evidence 脱敏；异常链与 frame locals 泄漏已修复，对应 change 已归档。
- FLOW-003 / ApprovalRecord trace：Phase 13.6A 已切换为 canonical run trace 非空生成、传播与历史 backfill；跨 tenant/不同 idempotency key 的同 trace 竞争由全局 trace 锁覆盖锁内复检、guardrail/audit 与 root claim，相同 event-id 仅允许除 seq/timestamp 外完整稳定语义一致的重试，terminal/approval 恢复先复用既有确定性 evidence。对应 changes 已归档。
- AC-064、AC-065：model/embedding evidence、stable semantic slot、queued run 执行前 recovery 与 local fake run `<5s` 已实现，完整门禁与阶段独立代码 1+2 已通过，对应 change 已归档。
- AC-015、AC-016：真实受控 delegation、child run、durable parent aggregation 与 local/service recovery 已实现并通过完整门禁及与 Phase 13.8A 的联合代码 1+2。
- AC-068：shared parent ledger、typed secret、0016 topology/source/price、错误优先级及审查追加项均已修复，并通过完整门禁、代码 1+2 与审查后收口。
- AC-038、AC-066：RUN-006 HTTP transport、Last-Event-ID 恢复与固定 30 样本首 frame P95 已实现并验证；WebSocket 仍为 P1。
- AC-050：由各 change 的 `tasks.md`、Phase 本地提交以及 review/test 命令证据持续维护 REQ/AC -> production -> test 追溯；Phase 15 release matrix 必须汇总并重新校验，新 change 仍需保留 red evidence。
- Phase 14 深度文档已交付并通过完整门禁；Phase 15 release automation 尚未实现。
- GitHub Actions / GitLab CI、CHANGELOG/tag/release dry-run 尚未实现。
- Phase 14 已补齐扩展指南、安全策略、维护者手册与当前/未来边界明确的 release 文档。

## 技术栈决策

Python dependency 的声明范围以各 `pyproject.toml` 为准，实际解析版本以 `uv.lock` 为准；Docker runtime 以 Compose image reference 为准。uv、Docker、Compose 等外部 CLI 未由仓库锁定，只能记录某次验证环境，不能冒充 lock 内容。本表给出当前受控事实和仍属后续 Phase 的目标。

| 层级 | 技术 | 版本 | 说明 |
|------|------|------|------|
| 运行语言 | Python | `>=3.12` | 由 `pyproject.toml` 声明；2026-07-20 Phase 14 本机验证为 3.14.5。Phase 15 才定义并证明 CI Python matrix。 |
| 包管理 / Workspace | uv | 仓库未锁定；2026-07-20 本机验证 `0.11.19` | 负责 workspace、lock 与 build；publish 命令虽由工具提供，但本仓库发布流程尚未实现。 |
| Build backend | hatchling | `1.30.1` | 现代 Python build backend，配合 `uv build` 产出 wheel/sdist。 |
| Agent runtime 底座 | pydantic-ai / pydantic-ai-slim | `2.5.0` | 默认底座，但业务 agent 只依赖 `agent_harness` 公共接口；优先使用 slim + extras 降低依赖面。 |
| Agent capability library | pydantic-ai-harness | 不作为 P0 必选依赖；按能力块引入时重新核验并锁版本 | 官方 capability library，用于 CodeMode、memory、guardrails、managed prompts、repo/filesystem tools 等可选能力；进入实现前必须走受控 integration boundary。 |
| 数据校验 | Pydantic | `2.13.4` | 配置、DTO、API schema、CanonicalEvent 和 adapter contract 的统一 schema 基础。 |
| HTTP API | FastAPI | `0.139.0` | 当前实现 `/api/v1/...`、OpenAPI、Swagger、Redoc、JSON events 与 RUN-006 SSE endpoint。 |
| ASGI Server | Uvicorn | `0.50.2` | service app 本地 API 入口；开发态使用 `uvicorn[standard]`，CI 可用基础安装。 |
| CLI | Typer | `0.26.8` | 实现 `agent-harness doctor/run/eval/policy/scaffold/approvals`。 |
| Durable execution | DBOS | `2.26.0` | service profile 默认 adapter；local profile 保留 SQLite-backed checkpoint。 |
| ORM | SQLAlchemy | `2.0.51` | 采用 2.0 typed declarative、async session、Repository + Unit of Work。 |
| Migration | Alembic | `1.18.5` | 统一 SQLite/PostgreSQL schema migration。 |
| PostgreSQL driver | asyncpg | `0.31.0` | service profile async driver；repository contract tests 以 async 路径为准。 |
| SQLite async bridge | aiosqlite | `0.22.1` | local profile 和 CI 使用 SQLite async adapter。 |
| Service database | PostgreSQL | Compose 默认 `postgres:18` | 只固定 major tag；具体 server patch/digest 由每次真实 service smoke 记录，不能写成仓库已 pin `18.4`。 |
| Durable queue | Redis server | `8.0.1` for Docker Compose | 当前只承担 Streams/XAUTOCLAIM RunQueue；session cache 属于 P1 可选能力。Phase 15 发布前重新核验镜像与许可证材料。 |
| Redis client | redis-py | `8.0.1` | Durable queue 只依赖 Redis Streams consumer group、claim/ack 与幂等状态 seam。 |
| Observability 底座 | OpenTelemetry Python | `observability` extra 解析为 `1.42.1` SDK/exporter | 以 `uv.lock` 为当前解析真相；OTel API/SDK 作为 provider adapter 前的统一协议。升级需重新解析 Logfire/Phoenix/Langfuse 组合。 |
| 推荐观测 provider | Logfire | `4.37.0` | 推荐 adapter；业务代码不直接 import。 |
| 可选观测 provider | Arize Phoenix | `17.21.0` | 2026-07-09 通过 PyPI 重新核验；可选 adapter contract，覆盖 trace/dataset/eval/feedback 工作流。 |
| 可选观测 provider | Langfuse Python SDK | `4.13.2` | 2026-07-09 通过 PyPI 重新核验；v4 SDK；adapter 层处理 v4 API，不污染核心接口。 |
| MCP client SDK | mcp | `>=1.28.1,<2` | 官方 PyPI 说明 v1 是稳定线、v2 是 alpha；P0 明确 `<2` 防止破坏性升级。 |
| HTTP client | HTTPX | `0.28.1` | MCP HTTP/SSE、provider adapter 和 smoke tests 使用。 |
| Lint / Format | Ruff | `0.15.20` | `make quality` 的 lint + format 主工具。 |
| Typecheck | Pyright | `1.1.411` | 使用 Python wrapper 或 npm pyright；CI 固定版本，避免自动漂移。 |
| Test runner | pytest | `9.1.1` | unit、contract、integration、smoke、eval tests 的统一 runner。 |
| Async tests | pytest-asyncio | `1.4.0` | runtime、storage、API、event stream 的 async tests。 |
| Coverage | coverage.py | `7.15.0` | 产出 CI coverage artifact；和 pytest 分离配置。 |
| Git hooks | pre-commit | `4.6.0` | 本地 quality hook 和 license/header check 入口。 |
| Release automation | 待 Phase 15 决策 | 尚未引入或锁定 | 当前没有 SemVer/tag/CHANGELOG/release artifact dry-run 或 publish automation；Phase 15 重新选型并验证 GitHub/GitLab 等价门禁。 |
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
              -> Phase 12.5 Eval experiment / harness hill-climb loop
                -> Phase 13 Service profile / split API-worker smoke
                  -> Phase 13.5 Run OpenAPI response/status accuracy
                    -> Phase 13.6 Config startup / Docker secret file
                      -> Phase 13.6A Canonical run trace correlation
                        -> Phase 13.7 Model / embedding usage evidence
                          -> Phase 13.8 Delegation execution / parent aggregation
                            -> Phase 13.8A Shared parent budget / migration hardening
                              -> Phase 13.9 SSE transport / Last-Event-ID / latency
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
- 为 ModelRouter、预算和 provider 配置预留显式 reload seam；P0 不要求 worker 运行中自动热重载。
- 实现 ContextAssembler：收口 history、retrieval、tool output、artifact refs、trust marker、token budget 和上下文降级链。
- 实现 EmbeddingProvider、mock/local embedding、OpenAI-compatible embedding adapter 和 embedding cache。

**关键文件**：
- `packages/agent-harness/src/agent_harness/registry/descriptor.py` - `AgentDescriptor`。
- `packages/agent-harness/src/agent_harness/registry/registry.py` - 多 agent 加载、校验、查询。
- `packages/agent-harness/src/agent_harness/models/router.py` - model routing、fallback、budget check。
- `packages/agent-harness/src/agent_harness/models/providers.py` - model provider interface。
- `packages/agent-harness/src/agent_harness/context/assembler.py` - ContextAssembler、history trimming、retrieval/tool output injection。
- `packages/agent-harness/src/agent_harness/context/assembler.py` - token budget、context truncation、fallback decision summary。
- `packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py` - Pydantic AI adapter。
- `packages/agent-harness/src/agent_harness/adapters/models/fake.py` - fake model provider。
- `packages/agent-harness/src/agent_harness/embeddings/provider.py` - embedding provider interface。
- `packages/agent-harness/src/agent_harness/storage/repositories.py` - embedding cache repository；provider seam 位于 `embeddings/provider.py`。
- `templates/service-app/app/api/routes/agents.py` - `/api/v1/agents`。
- `templates/service-app/agents/examples/basic/config.yaml` - registry smoke agent。

**验收标准**：
- `agent-harness agents list` 能列出已配置 agent。
- 重复 `agent_id` 或不合法 config 会被 registry 拒绝。
- fake model 下不需要真实 API key 就能跑测试和 eval smoke。
- ModelRouter / budget 配置变更有明确 reload seam 或 restart seam，不要求业务 agent 手读配置。
- ContextAssembler 生成 context assembly trace，能解释 source、trust_level、token budget、truncation 和 fallback decision。
- 业务 agent 不直接 import `pydantic_ai`；替换 fake adapter 后 contract tests 仍通过。
- `API-Contract.md` 的 `AGT-001` 已扩展为完整 endpoint 条目，OpenAPI drift test 覆盖 `/api/v1/agents` 的 route、schema、错误 envelope 和 registry validation error。

**实现状态**：
- 已实现 `agent_harness.registry` 的 `AgentDescriptor`、多 agent directory loader、重复 `agent_id` / invalid config 整体拒绝、public descriptor 禁止本地绝对路径 / secret / callable / provider client 泄漏，以及受控 delegation allow/deny/summary seam。
- 已实现 `agent-harness agents list` 和 service-app `GET /api/v1/agents`，二者通过同一 registry seam；`AGT-001` 已从保留索引扩展为完整 API 契约。
- 已实现 `ModelProvider`、`FakeModelProvider`、`PydanticAIModelProvider` adapter、`ModelRouter`、预算 fallback / policy-needed decision 和显式 reload seam；`pydantic_ai` import 只允许在 adapter 边界。
- 已实现 `ContextAssembler` 的 per-fragment trace、source/trust/token budget/truncation/fallback decision，并新增 `context_assemblies` 持久化表。
- 已实现 `EmbeddingProvider` Protocol、local/mock provider、OpenAI-compatible HTTP adapter 和 `embedding_cache` 持久化复用；service smoke 已证明 PostgreSQL/Redis profile 下 `embedding_cache=hit`。
- 实现和契约已归档到 `openspec/changes/archive/2026-07-08-agent-registry-model-context/`，主规格已同步为 `openspec/specs/agent-registry-model-context/spec.md`。
- 验证命令已通过：`uv sync`、`make quality`、`make test`（54 passed, 1 skipped）、`uv run pytest tests/contracts/test_agent_registry_model_context_contracts.py -q`（14 passed）、`uv run agent-harness agents list --agents-dir templates/service-app/agents`、`openspec validate agent-registry-model-context --type change --strict`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files` 和归档后 `openspec validate --all --strict`。

---

## Phase 7: 认证、PolicyEngine 与 HITL 审批

**交付内容**：
- 实现 API Key / Bearer Token 认证，注入 `IdentityContext`，未启用多租户时使用 default tenant/user。
- 实现 `PolicyEngine`、YAML provider、DB provider、默认危险动作策略和 audit log。
- 实现 approval required、approve/deny、checkpoint resume 的 HTTP/CLI 闭环。

**关键文件**：
- `packages/agent-harness/src/agent_harness/auth/tokens.py` - API key / bearer token verifier、token hash 和结构化认证错误。
- `packages/agent-harness/src/agent_harness/policy/engine.py` - policy decision、YAML provider、DB provider interface、InputGuardrail 和默认危险动作策略。
- `packages/agent-harness/src/agent_harness/approvals/service.py` - approval create/resolve/resume。
- `packages/agent-harness/src/agent_harness/audit/service.py` - audit log 写入。
- `templates/service-app/app/api/dependencies.py` - API 认证、policy、guardrail 和 approval dependency。
- `templates/service-app/app/api/routes/agents.py` - agent list 可见性 policy check。
- `templates/service-app/app/api/routes/runs.py` - run create `run.create` policy gate、InputGuardrail、approval/checkpoint 接入和 event visibility 过滤。
- `templates/service-app/app/api/routes/approvals.py` - approval API routes。
- `templates/service-app/app/api/routes/policies.py` - policy check API。
- `packages/agent-harness/src/agent_harness/cli.py` - policy check 和 approvals list/approve/deny CLI。
- `templates/service-app/configs/policy/default.yaml` - 默认策略配置。

**验收标准**：
- 无效 Bearer Token 调用 P0 API 返回认证错误且不创建 run。
- 未配置多租户时 run/session/trace/eval 均带 `tenant_id="default"`。
- shell、删除文件、workspace 外访问、写 approved dataset、修改 policy 等动作默认产生 `approval.required` 或被拒绝。
- approve 后 run 从 checkpoint resume，deny 后 run 按策略失败或 fallback，audit log 记录审批人、动作、结果和 trace。
- `API-Contract.md` 中 auth、policy、approval 相关 endpoint 已扩展为完整条目，局部 OpenAPI drift test 覆盖 401/403、`ApiErrorEnvelope`、approval 状态冲突和 request_id。

**实现证据**：
- OpenSpec change `auth-policy-hitl-approvals` 已通过 artifact review 和 code-reviewer Stage 1/2 PASS；主规格已同步到 `openspec/specs/auth-policy-hitl-approvals/spec.md`。
- API 契约已扩展 `APR-001`、`APR-002`、`POL-001` 和 `AGT-001`，并明确 `RunCreateResponse` 不暴露 `resume_token`、`PolicyDecisionResponse.audit_ref` 为必填、run create 前必须通过 `run.create` policy check。
- 存储层复用 `0001` 已有 `policy_rules` / `audit_logs`，并通过 `0003_auth_policy_hitl_approvals` 新增 `api_keys` / `approvals`；`make smoke-service` 已在 PostgreSQL/Redis 上验证 migration=`0003_auth_policy_hitl_approvals`。
- Phase 7 contract tests 已拆分为 auth/openapi、policy/guardrail/audit、approval API/CLI 和 event visibility 四组，覆盖无效 token no-side-effect、低权限 `run.create` 403、默认危险动作、approval 状态机、租户隔离、audit evidence 标准字段和 internal event 权限。
- 验证命令已通过：`uv sync`、`make quality`、`make test`（68 passed, 1 skipped）、`uv run pytest tests/contracts/test_auth_policy_hitl_openapi_contracts.py tests/contracts/test_auth_policy_hitl_*_contracts.py tests/contracts/test_typed_config_contracts.py -q`（18 passed）、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files`、归档前 `openspec validate auth-policy-hitl-approvals --type change --strict` 和归档后 `openspec validate --all --strict`（12 passed）。

---

## Phase 8: ToolRegistry、FileTool、ShellTool 与 MCP Client

**状态**：实现、全量验证、fresh code-reviewer Stage 1/2 PASS、本地提交和 OpenSpec archive 已完成；主规格已同步到 `openspec/specs/tool-execution-boundaries/spec.md`。

**交付内容**：
- 实现 `ToolRegistry`，统一本地工具、MCP 工具、schema validation、policy interception、trace/audit。
- 实现 Workspace FileTool：read/write/list/search/patch/delete，受 workspace 根目录、`.agentignore` 和 policy 控制。
- 实现 ShellTool 默认 disabled、显式启用、allowlist/denylist、timeout、stdout/stderr 截断、artifact_ref。
- 实现 MCP client connector：stdio、HTTP/SSE、tool discovery、allowlist、policy、untrusted output 标注和 trace/audit。

**关键文件**：
- `packages/agent-harness/src/agent_harness/tools/registry.py` - tool registry。
- `packages/agent-harness/src/agent_harness/tools/types.py` - tool request/result/error/descriptor DTO。
- `packages/agent-harness/src/agent_harness/tools/output_guard.py` - tool/MCP output source_ref、trust_level、截断和注入检测。
- `packages/agent-harness/src/agent_harness/tools/file_tool.py` - workspace file operations。
- `packages/agent-harness/src/agent_harness/tools/shell_tool.py` - guarded shell execution。
- `packages/agent-harness/src/agent_harness/tools/workspace.py` - workspace root、`.agentignore`、path guard。
- `packages/agent-harness/src/agent_harness/tools/cli_runtime.py` - CLI/runtime registry assembly、artifact/audit/persistence。
- `packages/agent-harness/src/agent_harness/tools/mcp_tools.py` - MCP tool wrapper。
- `packages/agent-harness/src/agent_harness/mcp/client.py` - MCP client interface。
- `packages/agent-harness/src/agent_harness/adapters/mcp/python_sdk.py` - official MCP SDK adapter。
- `packages/agent-harness/src/agent_harness/storage/tool_repositories.py` - workspace/tool invocation repository seam。
- `packages/agent-harness/src/agent_harness/storage/migrations/versions/0004_tool_execution_boundaries.py` - Phase 8 tool evidence migration。
- `templates/service-app/configs/tools.yaml` - tool allowlist / denylist / MCP server config。

**验收标准**：
- workspace 外路径默认被拒绝或要求审批。
- shell tool 默认 disabled；显式启用后仍受 allowlist、workspace 路径参数边界、timeout、环境变量白名单和 approval 控制。
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

**完成状态**：
- `retrieval-rag-foundation` 已通过实现、验证与 fresh code-reviewer Stage 1/2，由 `3c18297` 提交，并随 `45d87bf` 归档及同步到主规格。

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

**完成状态**：
- 已通过 `observability-provider-adapters` OpenSpec artifact review PASS、strict validate、Phase 10 targeted contract tests、全量质量/测试/smoke/build/license/pre-commit 验证和 fresh code-reviewer Stage 1/2 PASS。
- 已新增 `agent-harness[observability]` optional extra，锁定 Logfire/Phoenix/Langfuse 当前版本；因 Logfire 4.37.0 约束 `opentelemetry-sdk<1.43.0`，OTel SDK/exporter 在该 extra 内锁为可解析的 1.42.1。
- `observability-provider-adapters` 已由提交 `45d87bf` 归档并同步到主规格。

---

## Phase 11: Eval Gate 与 Trace 到 Eval 闭环

**交付内容**：
- 实现 `EvalCaseFactory`、failed/low-score detector、review queue、draft/approved dataset 分离和人工审核流程。
- 实现 `EvalRunner`、approved dataset 执行、ScoreSink、本地 JSONL score 和 provider score 写回。
- 接入 CLI/API：draft、approve、list、run eval、查看 score。

**关键文件**：
- `packages/agent-harness/src/agent_harness/evals/cases.py` - eval case model、draft/approved 状态。
- `packages/agent-harness/src/agent_harness/evals/cases.py` - trace source 到 draft case factory。
- `packages/agent-harness/src/agent_harness/evals/review_queue.py` - human review queue。
- `packages/agent-harness/src/agent_harness/evals/runner.py` - eval runner。
- `packages/agent-harness/src/agent_harness/evals/score_sink.py` - score sink interface。
- `packages/agent-harness/src/agent_harness/adapters/evals/local_jsonl.py` - local eval result sink。
- `templates/service-app/app/api/routes/evals.py` - eval API routes。
- `packages/agent-harness/src/agent_harness/cli.py` - eval CLI。
- `templates/service-app/eval-cases/drafts/.gitkeep` - draft dataset 目录。
- `templates/service-app/eval-cases/approved/.gitkeep` - approved dataset 目录。

**验收标准**：
- failed run trace 执行 `agent-harness eval draft` 后生成 draft case。
- 人工 approve 后 case 进入 approved dataset 并写 audit log；默认不允许自动写 approved dataset。
- `make eval` 只跑 approved cases，输出 eval result 和 score sink 记录。
- score 可写回 local/jsonl，并可通过 Logfire/Phoenix/Langfuse adapter contract 写入 provider。
- `API-Contract.md` 中 eval draft、approved dataset 和 eval run endpoint 已扩展为完整条目，局部 OpenAPI drift test 覆盖人工确认、secret 脱敏错误和 score sink 降级语义。

**当前状态**：
- `eval-gate-trace-loop` 已完成实现、本地验证，并由提交 `45d87bf` 归档及同步到主规格。
- `make eval` 已接入根 Makefile 和 service template；无 approved case 时返回 `no_approved_cases`，不会把 draft 纳入评分。

---

## Phase 12: Service App 模板与四个 P0 示例 Agent

**Change 关系与完成状态**：
- 实施顺序固定为 `service-app-template-surface` → `p0-example-agent-flows` → `agent-scaffold-cli`；三者共享 API/CLI composition、模板文档和最终验收，不能独立开工。
- 三个 change 的 tasks 分别为 `11/11`、`26/26`、`16/16`，实现与组合验收全部完成；最终 fresh code-reviewer Stage 1/2 PASS，审查备注也已在 `72e3fdf` 收口。
- 三个 change 及 Phase 9-11 changes 已由 `45d87bf` 统一归档，主规格完成同步，当前无 active change。

**交付内容**：
- 完成 `templates/service-app` 的 FastAPI、CLI、worker、configs、tests、docs、docker-compose 和 README。
- 实现四个薄样例 agent：RAG assistant、ticket triage、repo analyst、dev assistant，分别验证 retrieval、结构化输出、file tool、shell/HITL。
- 完成 `/api/v1/...` P0 endpoint、OpenAPI schema、Swagger/Redoc 管理面和 CLI 命令集。

**关键文件**：
- `templates/service-app/app/main.py` - FastAPI app。
- `templates/service-app/app/api/routes/*.py` - 由唯一 `create_app` factory 注册的 `/api/v1` health、agents、runs、policies、approvals 和 eval routes；不新建第二套总 router。
- `templates/service-app/app/api/routes/health.py` - health route。
- `templates/service-app/app/api/routes/agents.py` - agents routes。
- `templates/service-app/app/api/routes/runs.py` - run routes。
- `templates/service-app/app/api/routes/policies.py` - policy check route。
- `templates/service-app/app/api/routes/approvals.py` - approval list/read/resolve routes。
- `templates/service-app/app/api/routes/evals.py` - eval routes。
- `templates/service-app/app/cli/main.py` - Typer root CLI。
- `templates/service-app/app/workers/runtime_worker.py` - worker entry。
- `packages/agent-harness/src/agent_harness/runtime/executor.py` - provider-neutral `AgentExecutor`、typed result 和 `ApprovalGrant` seam。
- `packages/agent-harness/src/agent_harness/storage/migrations/versions/0008_agent_execution_approval_claims.py` - approval private resolution 与 tool execution claim migration。
- `packages/agent-harness/src/agent_harness/scaffold.py` - 安全、原子、可验证的 agent scaffold 实现。
- `packages/agent-harness/src/agent_harness/cli.py` - run composition、approval/eval/policy 既有入口和新增 scaffold group。
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
- 普通 checkpoint 可通过公开 `RUN-005` 恢复；approval-gated checkpoint 直接提交原始 resume token 必须返回 `409 run.invalid_transition`、不消费 token且 tool handler 计数为零，待批动作只能经 `APR-002` 私有 lease → `ApprovalGrant` → 内部 resume 执行。
- Approval-gated run 进入 waiting 后必须能在进程重启、使用同一持久化 storage 重建 registry/executor resolver/orchestrator/approval service 的条件下，经 `APR-002` approve 恢复原 continuation；handler 恰好一次，结果真实持久化且 terminal event 唯一，公开 resume token 不参与执行。
- `0008_agent_execution_approval_claims` 在 SQLite/PostgreSQL 上成为 latest migration，证明 approval 私有 resolution state、唯一 tool execution claim 与 service smoke；Phase 12.5 从 `0009` 开始，不复用 revision。
- 生产回滚保留 `0008` schema，只在停止新 resolve、清理所有未完成私有状态后回滚 application code，并证明上一版本 repository/UoW 兼容额外 nullable 列；Alembic downgrade 仅允许相关 resolution/claim 数据全空的 disposable 环境，非空时必须拒绝。
- 应用回滚 executor runtime 前必须 inventory 所有受管 `agents_dir`；存在仍依赖新 executor seam 的手工或 scaffold 生成 agent 时，preflight 列出 `agent_id` 并 fail-closed，必须保留 compatibility seam 或由操作者显式迁移/带审计隔离，禁止自动删除、改写和固定 fake fallback。
- 四个示例 fake model eval 均能确定性通过，且示例不直接 import 厂商 SDK。

**完成与归档状态**：
- `b77a028` 交付 Service App 模板表面，`698e2d4` 交付四示例与 approval execution，`ae4bba3` 交付安全 scaffold，`72e3fdf` 关闭最终审查备注。
- 实现、组合验证与最终 fresh code-reviewer Stage 1/2 均已通过；三个 change 随 `45d87bf` 归档并同步到长期主规格，Phase 12 已完成。

**收口验证证据**：
- `uv run pytest -ra`：`210 passed / 2 skipped`；两个 skipped 均要求 `AGENT_HARNESS_TEST_POSTGRES_DSN`。
- 注入真实 PostgreSQL DSN 后，repository service adapter 与 approval arbitration/lease fencing/unique claim 两项合同 `2 passed`。
- `make quality`、`make smoke-local`、`make smoke-service`、workspace 外 `smoke_template_copy.py --service`、`make eval`、`make build`、`make license-check`、`pre-commit --all-files` 全部通过；四示例 eval 为 11/11 approved cases通过、1 个 draft按设计跳过。
- 归档前三个 change 各自 `openspec validate <change> --type change --strict` 通过；归档后不存在 active change，长期主规格以当前 `openspec validate --all --strict` 结果为准。
- 最终 fresh code-reviewer Stage 1/2 PASS；后续审查备注修复再次通过 fresh review 后由 `72e3fdf` 提交。

**规格到代码影响矩阵**：

| 契约面 | 主要代码 surface | 收口审计与验证 | 完成证据 |
|---|---|---|---|
| Service App 表面 | `templates/service-app/app/main.py`、health/CLI、pyproject、Makefile、scripts、README | OpenAPI/422、copy-out wheel-only、health、service smoke | 通过；`b77a028` |
| Executor / Registry | `runtime/executor.py`、registry、orchestrator、CLI/API composition、全部 agent config | resolver 整体拒绝、真实 output、无 fake fallback、public descriptor 不泄漏 | 通过；`698e2d4` |
| Approval / Tool / `0008` | approval service、repository/UoW、models、migration、ToolRegistry、`RUN-005`/`APR-002` | 并发仲裁、重启恢复、handler 0/1 次、确定性 failed、needs-review、SQLite/PostgreSQL、forward-only 与受限 downgrade | 通过；`698e2d4` |
| 四示例与 Eval | 四个示例目录、EvalRunner adapter、ScoreSink/TelemetryFacade、docs | 四示例 run/eval、approved-only、citation/workspace/HITL、local-first provider degrade | 通过；`698e2d4` |
| Scaffold 与组合回滚 | scaffold module、核心 CLI、root discovery、README/tests、compatibility preflight | 原子发布、symlink/path 拒绝、生成后真实 run/eval、手工/生成 agent inventory、fail-closed rollback | 通过；`ae4bba3`，归档 `45d87bf` |

---

## Phase 12.5: Eval Experiment 与 Harness Hill-Climb 闭环

**进入条件**：
- Phase 12 已完成四个 P0 示例 agent，并且每个示例都有可运行 fake model eval 和 trace evidence。
- `eval-gate-trace-loop` 基础链路仍然保持人工 draft -> approve -> approved dataset -> eval run -> score sink；本 Phase 不允许把 draft 自动写入 approved dataset。

**交付内容**：
- 为 approved eval cases 增加 behavior tags、dataset metadata、optimization / holdout split 和 regression subset 管理。
- 实现 baseline experiment、candidate harness experiment、per-tag score comparison、holdout result 和 regression report。
- 建立 harness version metadata，覆盖 prompt、tool description、agent config、retrieval config、policy default 等会影响 agent 行为的变更输入。
- 增加人工 acceptance gate：只有分数提升、holdout 未明显退化、关键 regression 通过且 reviewer 明确接受后，候选 harness 才能进入 accepted record。
- 编写 trace mining / eval curation 指南，说明手写 case、生产 trace、外部数据集和饱和 case 清理的标准。

**关键文件**：
- `Product-Spec.md` - REQ-016 的 eval experiment / holdout / acceptance 要求。
- `API-Contract.md` - `EVL-004` experiment、comparison 和 accept endpoint 契约。
- `packages/agent-harness/src/agent_harness/evals/datasets.py` - behavior tags、dataset split 和 regression subset model。
- `packages/agent-harness/src/agent_harness/evals/experiments.py` - baseline/candidate experiment runner 和 comparison service。
- `packages/agent-harness/src/agent_harness/evals/recorded_evaluator.py` - 模板默认 approved-case 本地确定性 evaluator adapter。
- `packages/agent-harness/src/agent_harness/evals/harness_versions.py` - harness version metadata、checksum 和 diff summary。
- `packages/agent-harness/src/agent_harness/evals/acceptance.py` - 人工 review decision、policy/audit 绑定与 accepted production binding。
- `packages/agent-harness/src/agent_harness/storage/migrations/versions/0009_eval_experiment_loop.py` - experiment、split、accepted harness 基础 schema；`0010_eval_experiment_execution_claims.py` 以增量 revision 增加私有 execution claim/lease；`0011_eval_experiment_legacy_created_review.py` 在不改写已应用 0010 的前提下把结果不确定的 legacy `created` 转 `needs_review`；`0008` 已分配给 Phase 12 approval/tool execution claim。
- `templates/service-app/app/api/routes/evals.py` - `EVL-004` API routes。
- `packages/agent-harness/src/agent_harness/cli.py` - `agent-harness eval experiment ...` CLI。
- `packages/agent-harness/src/agent_harness/cli_eval_experiment.py` - CLI 与 HTTP 共用 service/policy/storage 的组合层。
- `docs/eval-observability-loop.md` - trace -> eval -> experiment -> harness acceptance 操作指南。
- `tests/contracts/test_eval_experiment_dataset_contracts.py`、`test_eval_experiment_storage_contracts.py`、`test_eval_experiment_migration_contracts.py`、`test_eval_experiment_postgresql_contracts.py` - split、repository、migration 与真实 PostgreSQL 合同测试。
- `tests/contracts/test_eval_experiment_comparison_contracts.py`、`test_eval_experiment_evidence_boundaries_contracts.py`、`test_eval_experiment_recovery_contracts.py`、`test_eval_experiment_acceptance_contracts.py` - comparison、evidence 上界、claim 恢复与 policy/audit 合同测试。
- `tests/contracts/test_eval_experiment_api_contracts.py`、`test_eval_experiment_api_acceptance_routes_contracts.py`、`test_eval_experiment_openapi_contracts.py`、`test_eval_experiment_cli_contracts.py` - HTTP、acceptance route、OpenAPI、CLI、secret/degraded 合同测试。

**验收标准**：
- approved cases 可按行为标签过滤，至少覆盖 tool selection、retrieval quality、follow-up quality、policy/approval、context/trust boundary。
- split 只读取 approved cases；draft case、secret 命中的 case 和缺少必需标签的 case 不得进入 optimization / holdout。
- baseline 和 candidate harness experiment 都记录 `harness_version`、`agent_id`、dataset split、score summary、regression summary、local/provider evidence ref。
- comparison report 输出 per-tag score delta、holdout delta、new failures、fixed failures 和 acceptance recommendation。
- accept 操作必须走人工 reviewer、policy decision 和 audit log；系统不得自动修改 prompt、tool description 或生产配置。
- 局部 OpenAPI drift tests 覆盖 `EVL-004` 的 create/read/comparison/accept endpoint、认证、422、409、provider degraded 和 idempotency 语义。
- `make eval` 继续保持基础 approved dataset 跑法；新增 experiment 命令不能破坏 Phase 11 的无 approved case 降级语义。

**2026-07-11 收口证据**：
- `uv run pytest -ra`：`281 passed, 3 skipped in 53.65s`；三个 skip 均为显式 `AGENT_HARNESS_TEST_POSTGRES_DSN` 条件合同，heartbeat 续租返回失败/抛异常在租约仍有效时 fail closed、过期租约拒绝终态写入、真实 create 持久化 draft rejected count，以及顶层/per-case 合法上界 refs 合并后稳定压缩的合同均通过。
- `AGENT_HARNESS_TEST_POSTGRES_DSN=postgresql+asyncpg://... uv run pytest <三个 PostgreSQL 合同>`：`3 passed in 1.30s`，分别覆盖 approval claim、通用 PostgreSQL repository adapter 和 Phase 12.5 migration/repository/downgrade。
- `make smoke-service`：真实 PostgreSQL/Redis healthy；先证明已有 0009 volume 原地升级到 0010，后续以同一已应用 0010 volume 前滚到 `0011_eval_experiment_legacy_created_review`；repository、eval experiment/acceptance、context、embedding、retrieval 和 worker probes 全部通过。
- `make quality`、`make smoke-local`、`make eval`、`make build`、`make license-check`、`uv run pre-commit run --all-files` 全通过；四示例 eval 共执行 11 个 approved cases，1 个 draft 被跳过，0 failures。
- `uv run openspec validate --all --strict`：`21 passed, 0 failed`；`git diff --check` 无输出。
- fresh code-reviewer 对三个 change 的单项与联合 Stage 1/2 均 PASS，HIGH/MEDIUM/LOW 均为 0；状态文档一致性修订纳入最终 fresh 审查范围，任何之后的受审 diff 都重置该门禁。

---

## Phase 13: Service Profile、API/Worker 分进程与未来拆分边界

**交付内容**：
- 完成 Docker Compose service profile，PostgreSQL、Redis、API 进程和 runtime worker 使用同一 storage/queue 配置协作。
- 验证 DBOS service adapter、shared checkpoint、event stream 和 run worker pickup。
- Redis queue adapter 的消息 header 必须传递 `request_id` 和 `idempotency_key`，避免 worker pickup 重试产生重复 run。
- 在代码和文档中固定未来微服务拆分顺序：先拆 worker，再拆 tool/model gateway，最后拆 observability/event pipeline；storage service 仅在 repository contract 稳定后拆；guardrail/context assembly 边界必须随 API/worker/model/tool gateway 保持 DTO/CanonicalEvent 兼容。

**关键文件**：
- `templates/service-app/docker-compose.yml` - PostgreSQL、Redis、API、worker。
- `templates/service-app/Makefile` - `smoke-service`、`migrate-service`、`worker`。
- `templates/service-app/app/workers/runtime_worker.py` - service worker 主循环。
- `packages/agent-harness/src/agent_harness/runtime/queue.py` - run queue interface。
- `packages/agent-harness/src/agent_harness/adapters/queue/redis.py` - Redis queue adapter。
- `packages/agent-harness/src/agent_harness/adapters/runtime/dbos.py` - DBOS workflow/checkpoint integration。
- `docs/architecture/README.md` - 当前同进程形态和未来拆分边界。
- `docs/adr/0001-p0-service-boundaries.md` - P0 不强制微服务但预留接口的决策。

**验收标准**：
- `make smoke-service` 能启动 PostgreSQL、Redis、API 和 worker。
- 分别启动 API 进程和 worker 进程后提交 run，run 被 worker 执行并产出 event stream。
- API 到 worker 的 queue message 保留 `request_id`、`idempotency_key`、`tenant_id`、`run_id`，Redis 重试不产生重复 run。
- API、worker、tool/model adapter 交换数据只使用 Pydantic DTO、CanonicalEvent、repository/provider/facade interface。
- API/worker/model/tool gateway 拆分后仍保留 source_ref、trust_level、context assembly trace 和 guardrail/audit 关联字段。
- 文档能让维护者指出 API、runtime worker、model/tool gateway、storage、event pipeline 的当前形态和未来拆分路径。

**完成状态**：
- 三个聚焦 OpenSpec change 已按 queue → split runtime → deployment proof 的依赖顺序实现、同步主规格并归档到 `openspec/changes/archive/2026-07-12-*/`。
- Compose 以 PostgreSQL、Redis、migration、API、worker 协作；API 与 worker 共享 storage/queue/DBOS/PostgreSQL event 配置，镜像只安装已构建 core wheel，不读取仓库源码。
- `make smoke-service` 已在 workspace 外复制模板中证明真实认证、RUN-001 四字段、worker A hard crash、worker B `XAUTOCLAIM`/同 DBOS workflow 恢复、唯一 terminal、approval enqueue 补投/checkpoint continuation、deny 零 continuation 与临时 credential 清理。
- 默认 smoke 不留随机 Compose container/volume；`SERVICE_APP_KEEP_DATA=1` 只保留指定 PostgreSQL volume，数据库复核临时 credential 为 0 后可用输出的精确命令删除。
- 架构 `.drawio`/`.excalidraw`/PNG、根/模板 README、API contract 与 `docs/adr/0001-p0-service-boundaries.md` 已同步；Phase 14/15 仍明确为待实现。
- 验证证据：Phase 13 聚焦合同 `54 passed`、真实 PostgreSQL/Redis/DBOS 集成 `12 passed`、离线全量 `325 passed, 13 skipped`、真实服务全量 `338 passed, 0 skipped`；quality、smoke-local、smoke-service、eval、build、license、pre-commit、OpenSpec strict 与 diff check 全通过。

---

## Phase 13.5: Run OpenAPI Response / Status 准确性

**交付内容**：
- 创建聚焦 change `run-openapi-contract-accuracy`，把 Product Spec AC-017 中已实现 RUN-001 到 RUN-005 的运行时 OpenAPI 准确性先收口；RUN-006 仍由 Phase 13.9 实现。
- 移除 run router 级共享 `responses`，按 operation 声明生产路径实际可返回的 status 与 `ApiErrorEnvelope`，防止不可能状态扩张公开契约。
- 保持 RUN-002 当前 `RunCreateResponse` 不变；Phase 13.8 切换 `RunDetailResponse` 时再以同一 change 内的 route、schema 和 drift test 原子更新。

**关键文件**：
- `templates/service-app/app/api/routes/runs.py` - RUN-001 到 RUN-005 operation-specific response map。
- `templates/service-app/app/main.py` - error handler、唯一 OpenAPI factory 与公开 status 对账。
- `tests/contracts/test_runtime_checkpoint_runs_contracts.py` - 精确 status/schema 集合，既检查缺失也拒绝额外 response status。
- `API-Contract.md` - RUN-001 到 RUN-005 当前 method/path/request/response/error/security 基准。

**验收标准**：
- 运行 OpenAPI 中 RUN-001 到 RUN-005 的 response status 集合与 `API-Contract.md` 逐 operation 精确相等，不再继承生产路径不可能返回的 `400/409/422/503`。
- 每个已声明错误 status 都引用 `ApiErrorEnvelope`；未声明 status 不得通过 router metadata 泄漏进 OpenAPI。
- RUN-002 在本 Phase 继续返回 `RunCreateResponse`；不得提前实现 Phase 13.8 的 delegation 聚合或 `RunDetailResponse`。
- 定向 contract tests、`make quality`、`make test`、`make smoke-local`、`make smoke-service` 和 `git diff --check` 通过。

**历史阶段状态**：归档前曾达到 `ready-to-archive`；10/10 tasks 已勾选，3 个 code-reviewer 的 Stage 1/2 均 PASS。定向 `56 passed`、离线 `330 passed, 13 skipped`，quality、local smoke、真实 PostgreSQL/Redis service smoke、change strict 与 diff check 均通过。对应 change 已于 2026-07-19 归档；AC-017 仍因 RUN-006 未实现而保持未完成。

---

## Phase 13.6: 配置启动失败与 Docker Secret File

**交付内容**：
- 创建聚焦 change `config-secret-file-loading`，把 Product Spec AC-008/063 与 API Contract CFG-001 固定为可测试行为。
- 在 typed settings 合并边界支持 `<BASE_ENV>_FILE`，拒绝 direct/file 冲突、相对路径、目录、symlink、越界、空值、非 UTF-8 和超限文件。
- 让 CLI、FastAPI、worker 和 migration composition 的缺失/无效配置统一结构化失败，且错误、doctor、health、日志和 evidence 不泄露 secret。

**关键文件**：
- `packages/agent-harness/src/agent_harness/config/settings.py`、`config/secret_files.py` - secret file 解析、冲突检查、合并顺序与受信文件读取。
- `packages/agent-harness/src/agent_harness/config/errors.py` - `config.secret_file_invalid`、冲突与安全提示。
- `templates/service-app/app/main.py`、`app/runtime.py`、`app/workers/runtime_worker.py` - application startup failure 映射。
- `templates/service-app/docker-compose.yml`、`.env.example`、`configs/profiles/service.yaml` - application DSN 与 PostgreSQL password 的只读 Docker secret file 装配，不提交或在 Compose config 展开真实 secret。
- `tests/contracts/test_typed_config_contracts.py`、新增 startup/config composition tests - CFG-001 与 AC-008/063。

**验收标准**：
- direct env、`.env` 和 `_FILE` 使用同一 typed field path；direct/file 同时配置稳定失败，不静默覆盖。
- 只读取显式受信 root 内普通文件，错误不包含 secret 内容或受信 root 外绝对路径。
- CLI/API/worker/migration 启动对缺失必填配置给出相同 code、field_path 和修复提示。
- wheel-only template contract、`make quality`、定向 tests、local/service smoke、OpenAPI 无漂移和 secret grep 通过；`docker compose config` 不包含 storage DSN 或 PostgreSQL password 原值。

**历史阶段状态**：归档前曾达到 `ready-to-archive`；13/13 tasks 已勾选，原始 Pydantic 异常链与 traceback frame locals 泄漏已修复并补回归测试，3 个 code-reviewer 的 Stage 1/2 均 PASS。对应 change 已于 2026-07-19 归档。

---

## Phase 13.6A: Canonical Run Trace Correlation

**交付内容**：
- 在 trace 开发前先完成聚焦 change `embedding-cache-tenant-isolation`：以插入式 revision `0012a_embedding_cache_tenant_scope` 修正 Phase 6 cache 的 tenant identity、跨租户 `vector_ref` 和持久化 hit/miss metadata，并把物理表切换为 `tenant_embedding_cache`，让旧 binary 在新 schema 上因旧表不存在而 fail closed；`0013` 直接依赖该 revision，已发布 `0013` 的事件 shape 漂移由线性后继 `0013a_run_trace_event_hardening` 收敛。
- 创建聚焦 change `run-trace-correlation`，收口 Product FLOW-003、ApprovalRecord 与 ModelUsageEvidence 对 `trace_id` 必填性的冲突。
- 每个新 root run 在任何事件、enqueue、tool/model/provider 副作用前绑定唯一 canonical trace；调用方缺失时由 runtime 生成，合法显式值保留，冲突值 fail closed。
- 把 canonical trace 持久化到 run execution context，并传播到 checkpoint/resume、worker、approval/audit、run-scoped CanonicalEvent 和后续 usage/delegation evidence。
- 对历史 nullable run/approval/event/audit 数据执行按 root lineage 确定性、幂等 backfill：单一合法非空 trace 优先且只填空，全空才按固定 namespace/root id 生成 UUIDv5，非法单值、多值或跨 lineage 碰撞在 DDL/UPDATE 前整批拒绝；binding 直接带 tenant，跨租户 parent edge与孤立记录同样 fail closed，多来源聚合 eval run 不伪造 AgentRun trace。
- 用 local state manifest 与单一 `agent-harness migrate-local-state` 离线入口冻结 SQLite、event JSONL、eval score JSONL inventory；普通运行入口不得自动推进旧 schema。

**关键文件**：
- `packages/agent-harness/src/agent_harness/runtime/` - trace normalizer、run create、execution context 与恢复传播。
- `packages/agent-harness/src/agent_harness/approvals/`、`storage/approval_records.py` - ApprovalRecord 非空 trace 与调用方不可覆盖。
- `packages/agent-harness/src/agent_harness/events/`、storage migration/repositories - run-scoped event trace 门禁与历史 backfill。
- local state manifest/upgrader 与 CLI - 显式 legacy inventory、journal/backup/fsync/atomic recovery 和 file-only eval fail-closed 边界。
- `templates/service-app/app/api/routes/runs.py`、CLI/runtime composition - HTTP 可选 `X-Trace-Id`、CLI 可选 `--trace-id` 与缺失生成。
- runtime/approval/event/API/CLI contract、SQLite/PostgreSQL integration 与 service worker recovery tests。

**验收标准**：
- API、CLI、内部入口缺失 trace 时生成；HTTP `X-Trace-Id` 与 CLI `--trace-id` 共用同一格式/冲突 normalizer，非法或已绑定其他 root run 的 trace 在业务副作用前返回稳定错误，CLI 只向 stderr 写稳定 code 并非零退出。
- 不同 tenant 或不同 idempotency key 并发竞争同一 trace 时，只有一个请求可进入 guardrail/root claim；失败方的 audit、event、queue 与 provider 副作用均为零。
- local、service worker、checkpoint/resume、approve/deny 与 terminal evidence 对同一 run 使用同一 trace，即使 request_id 改变。
- ApprovalRecord 与所有 run-scoped CanonicalEvent trace 非空且等于 persisted run context；调用方 body/metadata 无法覆盖。
- 相同 event-id 只有完整稳定事件语义一致时才可重放；除 seq/timestamp 外任一 envelope 差异都在 artifact/fan-out 前脱敏拒绝，terminal/approval 恢复只在既有 evidence 缺失时补写。
- SQLite/PostgreSQL backfill 幂等；单一合法历史 trace 不改写，全空 lineage 确定生成，非法单值、多值、全局碰撞、孤立记录和跨租户 parent edge 整批 fail closed；新 parent/child 与 trace binding 也有数据库租户门禁，migration 不删除历史 evidence。
- local manifest bundle 完整预检；中断后恢复全旧或继续全新，未登记路径与 `--file-only` 模式中的 run-scoped record 不得被声称已迁移。
- 定向 contracts、`make quality`、`make test`、local/service smoke、build、license、pre-commit、strict OpenSpec 与 diff check 通过。

**历史阶段状态**：归档前曾达到 `ready-to-archive` 基线；`ff81f91` 已提交 Phase 13.6A 的 tenant cache 与 canonical trace 实现。`embedding-cache-tenant-isolation` 与 `run-trace-correlation` 的实现、完整门禁及 code-reviewer Stage 1/2 均已通过，并于 2026-07-19 归档。

---

## Phase 13.7: Model / Embedding Usage Evidence 与 Local Latency

**交付内容**：
- 创建聚焦 change `model-usage-evidence`，实现 API Contract MOD-001 和 Product Spec AC-064/065。
- 为 model/embedding adapter 输出统一 `ModelUsageEvidence`，记录 `tenant_id`、provider/model、非负有限 token/cost/latency、cost availability、route/fallback/cache/budget decision 与 run/agent/trace；bool、负数、NaN/Infinity 和不一致的 `cost_status` 在持久化/聚合前拒绝。
- 通过 EventBus/TelemetryFacade 持久化 provider-neutral evidence；local fake run smoke 记录入口到 terminal 的总时延并执行 5 秒门禁。
- 以 `0013a_run_trace_event_hardening` 为直接前置增加 `0014` durable evidence outbox/usage settlement：provider 结果按 `usage_call_id` 幂等封闭，approval resolution 与 terminal 也按先前置 evidence、后 terminal 的稳定顺序恢复，绝不重放 provider/tool 副作用。

**关键文件**：
- `packages/agent-harness/src/agent_harness/models/providers.py`、`models/router.py`、新增 `models/usage.py` - 统一 evidence DTO 与路由语义。
- `packages/agent-harness/src/agent_harness/embeddings/provider.py`、`adapters/models/*`、`adapters/models/openai_compatible_embeddings.py` - provider mapping。
- `packages/agent-harness/src/agent_harness/events/types.py`、`observability/facade.py` - usage event/trace 映射与脱敏。
- storage `0014` migration、outbox/settlement repository、approval continuation/reconciliation - crash-safe usage 和 resolution/terminal 最终性。
- `templates/service-app/app/runtime.py`、示例 agent composition - 注入 run/agent/request/trace context，不让业务 agent 拼 raw usage。
- `tests/contracts/test_agent_registry_model_context_contracts.py`、observability/event contracts、`scripts/smoke_local.py` - MOD-001 与 AC-064/065。

**验收标准**：
- fake model、Pydantic AI adapter 和 embedding adapter 都产生同一 evidence shape；token/cost/latency 拒绝 bool、负数和非有限值；`reported|estimated` 必须有非负有限 `cost_usd`，`unavailable` 必须为 null，不得伪造 0。
- embedding cache hit 仍产生一组 started/final 调用级 evidence：当前 cache lookup 墙钟写入 `latency_ms`，token/cost 为 null + `unavailable`，decision 明确 `cache_status=hit`、`provider_called=false`，且不得复用首次 provider latency或再次调用 provider。
- `model.request.started` 与 `model.usage.updated` 关联同一 tenant/run/agent/trace；fallback 只调用实际备用 provider 并记录原决策与实际 provider/model，hard budget/policy 拒绝保持零 provider side effect；两类调用级最终 usage 都为 `terminal=false`，失败路径仍产出脱敏、可结算 evidence。
- sink 写失败或丢失确认后由 durable outbox 使用稳定 event id 恢复，provider/tool 不重放；terminal 可见前 usage 与 approval resolution 前置 evidence 已存在。
- prompt/embedding 原文、provider client/raw response 和 secret 不进入事件、trace、error 或公开 API。
- local fake run 的稳定入口级 smoke 在 5 秒内完成；阈值失败可重复定位而非依赖单元测试墙钟偶然性。

**历史阶段状态**：归档前曾达到 `ready-to-archive`，17/17 tasks；统一 usage DTO、stable semantic slot、真实 model/embedding runtime composition、`0014` durable evidence outbox/capacity、queued run 执行前 run-scoped recovery、approval resolution-before-terminal、双出口脱敏与 local `<5s` 均已实现并通过主控门禁和代码 1+2，已本地提交，并于 2026-07-19 归档。

---

## Phase 13.8: 真实受控 Delegation 与 Parent Aggregation

**交付内容**：
- 创建聚焦 change `agent-delegation-execution`，修正长期 OpenSpec 把 summary seam 写成真实执行的漂移，实现 DLG-001 与 AC-015/016。
- 提供内置 `agent.delegate` tool/module seam：registry edge、PolicyEngine、cycle/depth/budget、idempotency 和 tenant/identity 全部在创建 child 前门禁。
- local 复用 inline orchestrator，service 复用 durable RunQueue；child run 写 `parent_run_id`，parent detail 从持久化 child run、usage evidence 和 trace refs 计算 `DelegationSummary`。
- 先按 `(tenant,parent,key)` 与稳定 request hash 原子 claim 幂等请求，再让全新 claim 在同一事务按当前 parent 余额计算并持久化 parent 级 reservation；同 key 重试不重算动态余额，复用首次 reservation/operation，不同 key 串行竞争余额，并持久化结算/释放/needs_review 状态。
- 在 parent run 上发布固定 internal non-terminal delegation 生命周期：最多三条、稳定 event id、阶段 payload 与重放规则；CanonicalEvent 固定目录必须与 39 种代码枚举精确相等，terminal type/flag/visibility 必须双向一致。

**关键文件**：
- `packages/agent-harness/src/agent_harness/registry/registry.py`、新增 `registry/delegation.py` - edge 与 delegation service。
- `packages/agent-harness/src/agent_harness/runtime/_run_lifecycle.py`、storage run repository - parent/child 创建、查询与幂等。
- `packages/agent-harness/src/agent_harness/tools/`、`policy/engine.py` - `agent.delegate` tool 与 policy action。
- `templates/service-app/app/runtime.py`、worker composition、`app/api/routes/runs.py`、`app/api/schemas.py` - local/service execution 与 `RunDetailResponse`。
- `packages/agent-harness/src/agent_harness/events/types.py`、EventBus 与 local/PostgreSQL sink - 精确 event catalog、terminal 双向 guard 与零副作用拒绝。
- 新增 delegation contract/integration tests、service recovery smoke - deny/no-side-effect、allow、retry、failure、aggregation、生命周期顺序/重放/可见性和 needs_review 无 final。

**验收标准**：
- 未声明 edge、policy deny、cycle/depth/budget/tenant 失败均在 child run/queue/provider/业务事件副作用前拒绝；允许写一次脱敏 policy/audit denial evidence。
- 规范化 request hash 覆盖 tenant、identity、parent/source/target、child input 与稳定预算意图；P0 无显式预算参数时使用 `inherit_parent`，禁止把动态 parent 剩余额度或锁内计算的有效预留额写入 hash。新 claim 与首次 reservation 同事务提交；同 key 同 hash 即使其他 key 已改变 parent 余额也只复用首次 claim/reservation/child 和 durable operation，同 key 异 hash 在 reservation 前返回 `delegation.idempotency_conflict`，且零 child/queue/provider/业务事件副作用。
- 不同 key 并发竞争同一 parent 预算时由 SQLite/PostgreSQL 原子 reservation 串行化，合计不得超过剩余额度；未知最终 usage 保持 reserved/needs_review。
- service worker crash/reclaim 不重复执行 provider 调用或聚合 usage。
- parent detail 的 usage/budget/trace 只来自 durable child evidence，不接受调用方手填 summary。
- child failure 可追踪且不伪装 parent success；terminal/event seq/idempotency 与既有 runtime 契约不回归。
- Product/API/OpenSpec 的固定 event catalog 与 `CanonicalEventType` 精确等于 39 种；只允许三种 run terminal type 设置 `terminal=true`，且三种都必须 public/terminal，其他类型必须 internal/public 各自按契约保持 non-terminal；非法组合在 seq、容量、artifact 和 fan-out 前零副作用拒绝。
- 获准 delegation 按 claimed -> child.created -> completed|failed 最多三条发布；pre-child failure、needs_review 无 final、稳定 event id、parent run/trace/source agent、internal visibility、阶段 payload、敏感字段禁止与 local/service/reclaim 重放均逐值验收。

**状态**：主体实现和归档投影修正已完成，14/14 tasks；RUN-002 的最终合同已统一为 `RunDetailResponse`，与 Phase 13.8A 在同一冻结摘要上完成代码 1+2 及收口，并于 2026-07-19 归档。

---

## Phase 13.8A: Parent Execution-Tree Shared Budget 与 0016 Hardening

**交付内容**：
- 以 `shared-parent-budget-ledger` 收口 root execution tree 的单一 durable budget owner：direct model/embedding、delegation top-level claim 与 child allocation 竞争同一 token/cost hard limit；`max_cost_usd_per_run=null` 只关闭 shared cost 维度。
- 把 tenant-scoped keyed request fingerprint 改为 `BudgetSettings` typed secret，经 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` / `_FILE` 在统一 loader 中启动时 fail closed；runtime 不再直接读取 env/path，secret 从 settings payload、snapshot 和 evidence 中排除。
- 修复 `0016`：DDL 前扫描完整 parent topology，拒绝嵌套、孤儿、循环、跨租户或 relation 不唯一；未封闭 tree 必须从与 backfill bundle 分离的 durable immutable source evidence 回填；cost-enabled snapshot 的必需 prices 不得为 null。
- 固定 usage application UoW 错误优先级，确保 `event.sequence_state_invalid` 先于 budget；未封闭 claim/allocation、unknown 与 needs-review 继续 fence terminal。
- 让 `agent-delegation-execution` 的最终 RUN-002 MODIFIED requirement 唯一指向 `RunDetailResponse`，消除归档后 canonical spec 冲突。

**关键文件**：
- `Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`API-Contract.md`、`DEV-PLAN.md` 与相关 active OpenSpec changes。
- `packages/agent-harness/src/agent_harness/config/schemas.py`、`runtime/shared_budget.py`、service runtime composition 与 secret-file tests/smoke。
- `packages/agent-harness/src/agent_harness/storage/migrations/versions/_shared_parent_budget_0016/`、`0016_shared_parent_budget_ledger.py` 与 SQLite/PostgreSQL migration contracts。
- model/embedding invocation settlement UoW 及错误优先级 contracts。

**验收标准**：
- 缺失或非法 fingerprint secret 在四类 application startup 前结构化失败；valid env/file 精确保留内容语义，任何错误、日志、snapshot、evidence 和 traceback frame locals 不泄密。
- RUN-002 归档投影只保留一个 `P0 HTTP 契约与运行时 OpenAPI 无漂移` requirement，GET detail 的 200 response 唯一为 `RunDetailResponse`。
- SQLite 与真实 PostgreSQL 都在 DDL/UPDATE 前拒绝三层 parent、孤儿、循环、跨租户、relation 不唯一、自证 bundle、缺失独立 source evidence 与 cost-enabled null price；合法独立 evidence backfill 逐值一致。
- Embedding/model/delegation application UoW 按 `replay/conflict -> auth/owner/relation/snapshot -> sequence state -> budget -> capacity -> unique reread` 返回稳定错误且拒绝路径外部副作用为零。
- 完整 quality/test/local+service smoke/eval/build/license/pre-commit/strict validation 通过；第 1 位 fresh reviewer PASS 后才并行派第 2、3 位，三者对同一冻结 digest 的 Stage 1/2 全 PASS。

**状态**：27/27 tasks；归档审查 findings、生产修复、完整验证、同一冻结摘要的代码 1+2 与审查后收口均已完成，并于 2026-07-19 归档。

---

## Phase 13.9: SSE Transport、Resume 与首 Frame 性能

**交付内容**：
- 创建聚焦 change `sse-event-streaming`，实现 RUN-006、AC-038/066；WS 继续留在 P1。
- 新增 `text/event-stream` route，把 `CanonicalEvent` 映射为 id/event/data frame，并用唯一 `Last-Event-ID` 续读语义恢复。
- 新增 `agent-harness events stream <run_id>` CLI stream adapter；以 CLI 专属 `--after-seq` 续读同一授权 reader，并把 `canonical_event_bytes()` 逐条输出为 NDJSON，不另建事件状态。
- 复用 RUN-003 的 tenant/run/event visibility，区分握手前 ApiErrorEnvelope 与握手后 `stream.error`，terminal 后关闭。

**关键文件**：
- `templates/service-app/app/api/sse.py` - frame、heartbeat、stream error 映射。
- `templates/service-app/app/api/routes/run_events.py`、`app/api/routes/runs.py`、`app/api/schemas.py`、`app/main.py` - RUN-003/RUN-006 route 分组与 OpenAPI。
- `packages/agent-harness/src/agent_harness/cli.py` - `events stream` 命令、CLI cursor/visibility/error 与 NDJSON 输出；只调用授权 EventSink reader。
- event sink/read repository - 按 seq 增量读取，不新增第二套 event 真相源。
- EventBus/sink 与 `0014` evidence outbox - 固定 `1..2147483647` 写入容量，run 创建时预留 terminal，副作用前原子预留最大 prerequisite event 数；容量基数使用 `highest_persisted_seq` 而非 row count。公共 canonical JSON serializer 固定 UTF-8/排序键/紧凑分隔符/拒绝 NaN，正常 envelope 硬上限 `65536` bytes。SSE 使用同一 serializer 做 `100` event / `1048576` bytes 的受限分页，generator 同时只持有一个 page。
- 新增 SSE transport/OpenAPI/security tests、`scripts/smoke_local_events.py`、`scripts/smoke_local.py` 与 `service_sse_smoke.py` service probe。

**验收标准**：
- `Content-Type: text/event-stream`，frame `id` 等于 seq；`Last-Event-ID=n` 只发送 `seq>n` 的可见事件。
- CLI `events stream` 默认只输出 public event；`--after-seq=n` 只输出 `seq>n`，每行与公共 canonical bytes 逐值一致，terminal 后退出，空闲不输出 heartbeat 或伪造 event，Ctrl-C/错误不写业务 evidence。
- 默认隐藏 reasoning/internal；include_internal 需要权限；`Last-Event-ID` 与 OpenAPI header schema 固定 `0..2147483647`，非法或越界 header 在握手前结构化失败且无副作用。
- 握手后错误只发送脱敏 `stream.error`；terminal frame 后关闭，heartbeat 不占 seq。
- `highest_persisted_seq + outstanding reservations + terminal reservation` 在 run 级锁/CAS 下不得超过 `2147483647`，预约消费与 high-water mark 推进必须处于同一原子边界；稀疏高 seq 不能按 row count 低估。容量不足在 provider/tool/approval/delegation 副作用前以 `event.sequence_exhausted` 拒绝，未知结果保留预约并阻止 terminal，非法历史容量状态 fail closed。正常 envelope 按公共 canonical JSON serializer 超过 `65536` bytes 时必须 artifact 化或写前拒绝；legacy/direct-write 超限 row 只发送一次脱敏 stream error 后关闭。慢客户端逐 frame 等待 send，不预取第二页，断连后停止读取。
- P0 不增加 event 清理、TTL 或 retention job；run 存续期间 CanonicalEvent evidence 保留。未来 retention 必须另建 behavior change 定义 expired cursor，不能在 Phase 13.9 中隐式加入。
- 已存在可见 event 时首 frame 小于 1 秒；局部 OpenAPI drift、HTTP/CLI transport contracts、local/service transport smoke 和断线重连通过。

**状态**：实现、完整验证与最终 3-review 已完成，OpenSpec 任务 17/17；定向合同、local smoke、真实 PostgreSQL reader、PostgreSQL/Redis service smoke 与 30 样本首 frame P95 均通过，并于 2026-07-19 同步主规格后归档。

---

## Phase 14: 深度文档、ADR 与维护者指南

**交付内容**：
- 完成面向 app developer 和 scaffold maintainer 的 README、深度文档和 ADR。
- 写清 adapter contract、extension guide、security policy、guardrail / context assembly / trust boundary、eval-observability loop、release process 和目录禁止跨边界规则。
- 为每个能力块补充可执行命令、验收证据位置和常见故障排查。

**关键文件**：
- `README.md` - 根 README 最终版。
- `docs/architecture/README.md` - 架构和未来拆分边界。
- `docs/extension-guide.md` - 扩展 agent、tool、model、retrieval、observability、eval adapter。
- `docs/adapter-contracts.md` - provider/repository/facade contract。
- `docs/context-and-trust-boundary.md` - Agent Loop、HITL 回边、SSE/WS 回传、ContextAssembler 和 untrusted input 处理。
- `docs/eval-observability-loop.md` - trace -> eval -> score -> provider 闭环。
- `docs/security-policy.md` - auth、policy、approval、workspace、secret redaction。
- `docs/release-process.md` - 当前人工质量/构建/license 边界，以及 Phase 15 的 SemVer、tag、CHANGELOG、private publish 未来合同。
- `docs/adr/0002-vendor-adapter-isolation.md` - 上游隔离决策。
- `docs/adr/0003-redis-runtime-license-policy.md` - Redis runtime pin 与 license review 决策。

**验收标准**：
- AC-049：维护者阅读 docs 后能找到 adapter contract、release process、安全策略、context/trust boundary、ADR 和 eval/observability 闭环。
- 新开发者阅读 README 后能运行 local profile、理解目录职责和禁止跨边界规则。
- 所有文档命令都能在当前 repo 执行或明确标注需要 service profile。
- 文档中的技术栈版本和 `pyproject.toml` / `uv.lock` 保持一致。

**状态**：README、八份深度文档入口与三份 ADR 已互链；内部路径/锚点、四个官方外链、dependency lock、Compose image 与未锁定外部 CLI 已分别核验。quality 通过；全量 `1001 passed, 222 skipped`；approved eval `11/11`；local smoke、从干净复制模板逐字执行的 fingerprint key/migration/smoke-local/run-basic/dev-health Quick Start、真实 PostgreSQL/Redis/DBOS service smoke、build、license、OpenSpec strict 和 diff check 均通过。`maintainer-deep-documentation` 的 14/14 tasks 已同步到 `openspec/specs/maintainer-documentation/spec.md` 并归档到 `openspec/changes/archive/2026-07-19-maintainer-deep-documentation/`；Phase 15 保持未开始。

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
- `Makefile` - `quality`、`test`、`integration`、`eval`、`smoke-local`、`smoke-service`、`build`、`license-check` 的稳定入口。
- `templates/service-app/pyproject.toml` - 声明兼容的 `agent-harness` 版本范围，例如 `>=0.1,<0.2`。
- `scripts/license_check.py` - license / NOTICE / vendoring 检查。
- `scripts/import_boundary_check.py` - import boundary CI 检查。
- `scripts/release_dry_run.py` - release preview wrapper。
- `CHANGELOG.md` - generated changelog 输出。
- `docs/release-process.md` - release 操作文档。
- `docs/p0-acceptance-matrix.md` - P0 验收矩阵和证据链接。

**验收标准**：
- AC-050、AC-051、AC-053、AC-054：GitHub CI 和 GitLab CI 都分别运行 `make quality` 与 `make test`，并运行 `make integration`、`make eval`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`；各门禁有独立结果并产出 test report、coverage、trace sample、eval result、smoke logs、wheel/sdist、release preview artifact。
- AC-055：有 releasable commits 时 release dry-run 能生成下一版本、tag 名称、CHANGELOG 预览和 wheel/sdist artifact。
- AC-056：无 releasable commits 时 release dry-run 不创建 tag 或 release。
- AC-058：`LICENSE` 为 Apache-2.0，`NOTICE` 可追踪第三方声明，license check 能阻止未声明 vendoring 或不兼容 license。
- 模板依赖声明使用可发布的兼容版本范围，例如 `agent-harness>=0.1,<0.2`，不得把 workspace path 依赖带入发布产物。

---

## 数据库表

| 表名 | 所属 Phase | 用途 |
|------|-----------|------|
| `tenants` | Phase 3 | 默认租户和未来多租户隔离基础。 |
| `identities` | Phase 7 | API key / bearer token 解析后的身份记录或本地默认身份。 |
| `sessions` | Phase 3 | 用户会话和 agent session 关联。 |
| `agent_runs` | Phase 3 / 13.6A / 13.8 | run 生命周期、状态、parent run、idempotency；Phase 13.6A 增加非唯一 canonical trace 投影，Phase 13.8 让 parent/child 归属进入真实 delegation 执行。 |
| `run_trace_bindings` | Phase 13.6A (`0013`) | 直接持久化 `tenant_id`，以复合 root/tenant 约束把全局唯一 `trace_id` 绑定到唯一 root run；child 通过 `agent_runs.trace_id` 复用 root lineage。 |
| `checkpoints` | Phase 3 / 13.6A | durable runtime checkpoint 和 resume token；Phase 13.6A 回填并强制继承 canonical trace。 |
| `canonical_events` | Phase 4 / 13.6A / 13.7 / 13.9 | run event、seq、terminal、visibility；Phase 13.6A 回填并强制 run-scoped canonical trace，后续补 model/embedding evidence 与 SSE 单一事件真相源。 |
| `trace_refs` | Phase 4 / 13.6A | local/provider trace 引用；Phase 13.6A 新增独立 canonical `trace_id`，不覆盖 provider `external_trace_id`。 |
| `artifacts` | Phase 4 | 大 payload、tool output、eval evidence、checksum。 |
| `embedding_cache` -> `tenant_embedding_cache` | Phase 6 / 13.6A 前置修复 (`0012a`) | `0012a` 保留 row/evidence 后切换物理表名；tenant、embedding 输入 hash、provider、tenant-scoped vector ref、最近一次 hit/miss 与首次 provider latency metadata 使用四列 identity。旧 binary 继续查询 `embedding_cache` 时因表不存在而 fail closed，不能恢复跨租户读取。 |
| `api_keys` | Phase 7 | API Key / Bearer Token 本地认证材料的 hash 和权限范围。 |
| `policy_rules` | Phase 7 | YAML/DB policy provider 的规则落库。 |
| `approvals` | Phase 7 / Phase 12 / 13.6A | HITL approval required / approve / deny 记录；Phase 12 增加不进入 public DTO/OpenAPI 的 private resolution lease/state，Phase 13.6A 回填并收紧 canonical trace。 |
| `audit_logs` | Phase 7 / 13.6A | policy decision、approval、tool、eval dataset 写入审计；Phase 13.6A 对 run-scoped payload 回填并校验 canonical trace。 |
| `guardrail_checks` | Phase 7 | input/tool/retrieval guardrail 检查摘要、decision、source_ref 和 artifact_ref；Phase 4 先以 CanonicalEvent/local evidence 表达。 |
| `context_assemblies` | Phase 6 | context input refs、token budget、trust summary、truncation summary 和 output_ref。 |
| `workspaces` | Phase 8 | per-run 或 per-agent workspace 根路径和 policy 引用。 |
| `tool_invocations` | Phase 8 / Phase 12 / 13.6A | tool name、args_ref、result_ref、status、duration；Phase 12 增加 nullable unique `approval_id`、arguments hash、execution state/result ref，作为 approved continuation 的持久化单次执行 claim；Phase 13.6A 对 run-scoped invocation 回填并校验 canonical trace。 |
| `retrieval_documents` | Phase 9 | RAG 示例和 local/service retrieval 的文档 metadata。 |
| `retrieval_chunks` | Phase 9 | chunk 文本 ref、BM25/vector metadata、citation ref。 |
| `eval_cases` | Phase 11 / 13.6A | draft / approved eval case；具有 run 归属的记录由 Phase 13.6A 对齐 canonical trace，人工非 run draft 保持独立。 |
| `eval_runs` | Phase 11 / 13.6A | 一次 eval 执行的状态和 score summary；Phase 13.6A 仅为自身 `run_id` 非空的记录投影对应 canonical trace，多来源聚合 eval run 保持非 run nullable 语义。 |
| `eval_scores` | Phase 11 / 13.6A | per-case / per-metric score 和 provider ref；run-scoped 历史记录由 Phase 13.6A 回填。 |
| `run_evidence_outbox` | Phase 13.7 (`0014`) | usage settlement、approval resolution 与 terminal 的稳定 event id、有序发布和 crash recovery；不保存 raw provider/tool payload。 |
| `agent_delegations` | Phase 13.8 (`0015`) | parent/child、request hash、幂等 claim、状态与 evidence refs；RUN-002 以其中非空 `child_run_id` relation 决定 summary membership，不依赖 aggregate row 是否已生成。 |
| `delegation_budget_reservations` | Phase 13.8 (`0015`) | parent 级 token/cost reservation、结算/释放与 needs_review 状态。 |
| `delegation_aggregates` | Phase 13.8 (`0015`) | child terminal/model evidence 的可重入结算投影与 incomplete/needs_review 状态；只补充已结算数值，不决定 child 是否存在。 |
| `parent_budget_ledgers` | Phase 13.8A (`0016`) | 以非空同 tenant root `budget_owner_run_id` 冻结 execution-tree shared token/cost hard limits、cost-enabled 与完整 root/target snapshot。 |
| `budget_operation_claims` | Phase 13.8A (`0016`) | root direct 与 delegation top-level operation 的 stable key、immutable identity、opaque fingerprint、reservation/actual、side-effect 与 recovery 状态。 |
| `delegation_budget_allocations` | Phase 13.8A (`0016`) | delegation 内 child model/embedding allocation；受 top-level reservation 与 parent ledger 双重约束，避免 parent aggregate 双计。 |
| `eval_dataset_splits` | Phase 12.5 | behavior tag、optimization / holdout / regression subset 和 case membership。 |
| `eval_experiments` | Phase 12.5 | baseline/candidate harness experiment、score delta、holdout result 和 comparison evidence。 |
| `harness_acceptance_records` | Phase 12.5 | 人工 acceptance decision、policy decision、audit ref 和 accepted harness version。 |
| `release_records` | Phase 15 | version、tag、CHANGELOG、artifact、commit sha。 |

## Spec 覆盖矩阵

| Product-Spec 条目 | 覆盖 Phase |
|---|---|
| REQ-001 Monorepo / uv workspace | Phase 1 |
| REQ-002 核心包与上游隔离 | Phase 2, Phase 6, Phase 10, Phase 12 |
| REQ-003 后端服务型模板 | Phase 1, Phase 12 |
| REQ-004 配置系统 | Phase 2, Phase 12, Phase 13.6, Phase 13.8A（fingerprint typed secret） |
| REQ-005 存储、迁移与事务边界 | Phase 3, Phase 12, Phase 13.6A, Phase 13.8, Phase 13.8A |
| REQ-006 Durable runtime、checkpoint、resume | Phase 5, Phase 12, Phase 13, Phase 13.6A, Phase 13.8, Phase 13.8A |
| REQ-007 多 agent registry 与 delegation | Phase 6（registry/summary seam）, Phase 13.8（真实执行/聚合）, Phase 13.8A（共享 owner/allocation） |
| REQ-008 API、CLI 与管理面 | Phase 5, Phase 7, Phase 11, Phase 12, Phase 13.5, Phase 13.8, Phase 13.9 |
| REQ-009 租户、身份与认证 | Phase 2, Phase 7, Phase 13.6A, Phase 13.8, Phase 13.9 |
| REQ-010 PolicyEngine、权限拦截、InputGuardrail 与 HITL | Phase 2, Phase 4, Phase 7, Phase 12, Phase 13.6A, Phase 13.8, Phase 13.9 |
| REQ-011 工具系统、Shell、File、MCP | Phase 8, Phase 12 |
| REQ-012 模型、预算、上下文组装与 embedding | Phase 2, Phase 4, Phase 6, Phase 13.6A, Phase 13.7, Phase 13.8A |
| REQ-013 Retrieval 与 RAG | Phase 6, Phase 9, Phase 12 |
| REQ-014 CanonicalEvent 与流式输出 | Phase 4, Phase 5, Phase 13.6A, Phase 13.7, Phase 13.8, Phase 13.8A, Phase 13.9 |
| REQ-015 Observability 转换层 | Phase 4, Phase 10, Phase 13.6A, Phase 13.7, Phase 13.8 |
| REQ-016 Eval Gate 与 trace/eval 闭环 | Phase 10, Phase 11, Phase 12.5 |
| REQ-017 示例 agent | Phase 12 |
| REQ-018 README 与文档体系 | Phase 1, Phase 12, Phase 14 |
| REQ-019 TDD、测试与质量门禁 | Phase 1, all phases；AC-050 由各 change tasks、Phase 本地提交、review/test 命令证据与 Phase 15 release matrix 持续覆盖，AC-051、AC-053、AC-054、AC-055、AC-056、AC-058 由 Phase 15 覆盖，AC-065、AC-066 由 Phase 13.7、Phase 13.9 固定性能证据 |
| REQ-020 CI/CD 与 Release Automation | Phase 15 |
| REQ-021 开源合规与许可证 | Phase 1, Phase 14, Phase 15 |
| REQ-022 部署边界与未来微服务拆分基础 | Phase 2, Phase 4, Phase 5, Phase 13, Phase 13.8, Phase 13.9, Phase 14 |

## 开发规则

- 包管理器只用 `uv`；不使用 poetry、pipenv、npm 作为 Python 依赖主流程。
- 每个 Phase 必须先有失败测试或 contract test，再实现代码；不接受先堆代码后补测试作为 Phase 完成方式。
- 新增或修改 HTTP endpoint 必须先更新 `API-Contract.md`，再新增局部 OpenAPI drift contract test，最后实现 route；发布前全量复扫只做证据汇总，不作为第一次发现契约问题的入口。
- 同一目标的多个 OpenSpec change 只要共享 run/event/config/测试或验收，就必须在任何实现开始前完成逐 change 严格校验，并由 3 个 fresh code-reviewer 按 1+2 执行同一冻结范围的联合审查：第 1 名对每个 change 及组合范围给出 Stage 1/2 PASS 后，才并行派第 2、3 名；三名都必须同时给出逐 change 与联合 PASS。任一 finding、修复或受审 diff 都使既有 PASS 失效并从第 1 名重启，不再重复建立一套与联合审查分离的逐 change reviewer 队列。本轮 13.5-13.9 的八个关联 change 已按依赖顺序完成并归档，其中 `embedding-cache-tenant-isolation` 是 Phase 13.6A 的强制前置。
- Phase 13.5-13.9 实现严格按 `13.5 -> 13.6 -> 13.6A -> 13.7 -> 13.8 -> 13.8A -> 13.9` 串行。后序 change 在前序未归档 diff 上继续工作并必须重跑全部受影响合同；不得并行编辑共享文件，也不得用后序 change 覆盖前序已通过行为。
- 共享文件按接力所有权收口：`app/api/routes/runs.py` 由 13.5 建立精确 response map、13.6A 固定 trace header/生成、13.8 扩展 detail、13.9 最终加入 SSE并保留累计合同；`app/api/schemas.py` 由 13.8 建立 delegation schema、13.9 只追加 SSE 所需 schema；`app/runtime.py` 依次承接 13.6 startup、13.6A trace、13.7 usage、13.8 delegation；`app/main.py` 由 13.5 先接入窄化 OpenAPI factory、13.6 固定 startup、13.6A 接入 trace normalizer、13.9 再装配 SSE；`scripts/smoke_local.py` 由 13.6A 先固定 run trace、13.7 再固定 fake latency、13.9 最终追加首 frame 门禁。每次 Phase 接力后的 1+2 审查覆盖累计行为。
- 共享 storage/event 文件同样按串行接力：`storage/models.py`、`storage/repositories.py`、`storage/migrations/versions` 与 migration contracts 先以 `0012a_embedding_cache_tenant_scope` 闭环 Phase 6 cache 租户隔离并把物理表切换为 `tenant_embedding_cache`；再由 13.6A 的 `0013` 建立 trace binding/backfill，并以 `0013a_run_trace_event_hardening` 在不改写已应用 revision 的前提下统一旧/fresh 事件 shape；13.7 的 `0014` 必须直接依赖 `0013a`，增加 durable evidence outbox/usage settlement与 event capacity reservation并把 approval resolution/terminal 纳入有序恢复，13.8 在 `0015` 追加 delegation/aggregation/budget reservation，13.8A 的 `0016` 再引入 shared owner ledger/top-level claim/child allocation并在 DDL 前完成全拓扑和独立历史证据 preflight。`0013a -> 0013` 只回退 stamp并保留硬化 schema；其余破坏性 downgrade 仍必须满足 evidence 全空和精确 Alembic opt-in。
- 每完成一个 Phase 执行四步走：Code Review -> 测试完整性 -> 编译验证 -> 功能测试。
- 四步走全部通过后才能 commit；commit message 用 `feat`、`fix`、`refactor`、`chore` 前缀。
- `packages/agent-harness` 不依赖 `templates/*` 或 `examples/*`；模板只能通过 path dependency 或 wheel 使用核心包。
- 业务 agent、模板 app、eval runner 不直接 import Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 或直接操作 SQLAlchemy session。
- 所有跨边界数据必须使用 Pydantic DTO、CanonicalEvent、repository interface、provider interface 或 facade。
- local/jsonl 永远可用；外部 provider 失败不得导致本地证据丢失。
- 危险动作默认走 policy 和 approval；不得为了测试方便绕过 PolicyEngine。
- 外部输入、MCP output、tool output、retrieval chunk 默认按 untrusted 处理；进入模型上下文必须经过 ContextAssembler 并保留 source_ref、trust_level 和 truncation metadata。
- `eval-cases/approved` 只能由审核流程写入；自动 detector 只能写 draft。
- Phase 12.5 开始后，harness 变更接受必须基于 baseline/candidate comparison、holdout result、regression summary 和人工 review；不得只看总分上涨就接受。
- 所有核心数据、trace、eval、audit、artifact 必须带 `tenant_id`，run 相关数据必须带 `agent_id`、`run_id` 或 `trace_id`。

## 已知风险与限制

| 风险 | 影响范围 | 处理 Phase | 当前状态 | 处理方式 / 验收信号 |
|------|----------|------------|----------|----------------------|
| Pydantic AI 2.5.0 刚发布，上游 API 和包边界可能变化。 | 核心 runtime、registry、model adapter 和业务 agent import 边界。 | Phase 2、Phase 6、Phase 10 | 已缓解 | Phase 2 已定义 `agent_harness` 公共契约和 vendor import 边界；Phase 6 已锁定 `pydantic-ai==2.5.0`，并把 `Agent.run_sync()` 调用隔离在 `agent_harness.adapters.models.pydantic_ai`。 |
| Pydantic AI Harness 是独立可选 capability library，过早设为必选会扩大依赖面。 | CodeMode、memory、guardrails、managed prompts、repo/filesystem tools 等未来 capability integration。 | Phase 8、Phase 10、Phase 14 | 未处理 | P0 不直接依赖 `pydantic-ai-harness`；只有具体能力块需要时才新增 adapter/integration seam、锁定版本并扩展 import boundary 检查。 |
| DBOS 2.26.0 是关键 service runtime 依赖，过早耦合会污染领域模型。 | Durable runtime、checkpoint、worker lifecycle。 | Phase 5、Phase 13 | 已缓解 | Phase 13 通过 `DBOSRuntimeAdapter`、稳定 executor/workflow identity 与 shared checkpoint 隔离；内部 run/checkpoint DTO 不依赖 DBOS 类型。 |
| Redis runtime 版本与许可证变化影响 Apache-2.0 发布合规判断。 | Docker Compose service profile、durable queue adapter、发布合规。 | Phase 13、Phase 15 | 已缓解 | Phase 13 Compose 与 client 固定 `8.0.1` 并通过 license check；后续升级与发布仍必须走 ADR、NOTICE 与 license review。 |
| PGroonga 和 pgvector 是 optional adapter，可能拖累 local profile 或 CI。 | Retrieval、embedding cache、service profile smoke。 | Phase 9、Phase 13 | 已缓解 | Phase 9 已把 PGroonga/pgvector 作为 optional capability probe；local profile 不硬依赖扩展，service smoke 输出缺失降级提示并继续走 PostgreSQL native FTS fallback。 |
| P0 只做可拆边界，不做完整微服务；如果 API/worker/storage/tool 边界不清，后续会重构。 | API、runtime worker、model/tool gateway、storage、event/observability。 | Phase 2、Phase 4、Phase 5、Phase 13、Phase 14 | 已缓解 | Phase 13 已物理拆分 API/worker并用 DTO、CanonicalEvent、repository/provider seam 固定当前所有权；tool/model、event pipeline、storage 仍按文档顺序保留为未来边界。 |
| Phoenix、Langfuse、Logfire 的 dataset/score/workflow 能力差异大。 | Observability adapter、Eval Gate、score sink。 | Phase 10、Phase 11 | 未处理 | P0 先做 provider-neutral contract 和 local/jsonl fallback；复杂 provider-native workflow 放 P1。 |
| Eval 只用已知 case 做优化会过拟合，尤其是示例 agent 数量少时。 | Eval experiment、harness prompt/tool description/config 变更、release gate。 | Phase 12.5、Phase 15 | 部分缓解 | Phase 12.5 已按 behavior tags 拆分 optimization / holdout、保留 regression subset，并用人工 review 拦截无意义或过拟合的 harness 变更；Phase 15 仍需把这些证据接入 release gate，并持续扩充生产分布 case。 |
| Prompt injection / tool output injection 如果后补，会污染所有 agent 和 eval 证据。 | Access input、MCP、tools、retrieval、context assembly、audit。 | Phase 2、Phase 4、Phase 6、Phase 8、Phase 9 | 已缓解 | Phase 2 已定义 trust marker/source_ref/context ref 和 guardrail decision DTO；Phase 6 已在 ContextAssembler 保留 per-fragment source/trust/token/truncation trace；Phase 8 已处理 tool/MCP output；Phase 9 已让 retrieval chunk 进入 context 前保留 citation/source_ref/trust_level，prompt injection 文本只作为 untrusted citation 内容。 |
| Docker secret file 若直接当普通路径读取，会引入 symlink/越界、冲突优先级和错误泄密。 | settings、API/worker/migration startup、doctor/health/log。 | Phase 13.6 | 已缓解 | CFG-001 已实现受信 root、普通文件、64 KiB、direct/file 冲突、四入口结构化失败；真实 service smoke 扫描 health、doctor、logs、PostgreSQL 与 artifacts，并证明成功、失败和中断清理。 |
| Shared-budget fingerprint 若由 runtime 自行读取 env/path，会绕过 typed secret 的受信根、大小、冲突和脱敏门禁。 | settings、runtime/service composition、snapshot/evidence。 | Phase 13.8A | 已缓解 | 已改为 `BudgetSettings` secret 字段并由 CFG-001 loader 唯一注入；四启动入口、wheel-only、Compose 与 traceback locals 已共同验证 fail closed 和原值不落 payload。 |
| `0016` 只扫描 root/direct child 或允许 self-contained bundle 自证，会让嵌套/孤儿/cycle、current-config 漂移和错误历史身份进入 shared ledger。 | Alembic、legacy recovery、SQLite/PostgreSQL。 | Phase 13.8A | 已缓解 | 已在 DDL 前验证全表 topology；未封闭 tree 要求与 backfill bundle 分离的 durable immutable source evidence，逐值核对 snapshot/identity/hash/version，缺失或冲突整批拒绝。 |
| Cost-enabled snapshot 允许 null price 或 usage UoW 先返回 budget，会把不可证明 reservation 当成合法并遮蔽 event sequence corruption。 | model/embedding reservation、migration、recovery。 | Phase 13.8A | 已缓解 | 必需 route price 已要求非 null、非 bool、非负有限；sequence-state 固定在 budget/capacity 前，并由零外部副作用合同覆盖。 |
| Run/approval/event 允许空 trace 或宽松 event-id 重放会让审计、usage 与 delegation 产生不兼容关联。 | runtime、worker、checkpoint、approval、CanonicalEvent、model usage。 | Phase 13.6A | 已缓解 | `run-trace-correlation` 已在副作用前生成 canonical trace，以全局锁覆盖跨 tenant/不同 key 竞争，跨进程传播并确定性 backfill；合法 non-run evidence 保留独立 nullable trace，数据库三列复合外键守住 run/tenant/trace 联合归属，完整事件指纹与既有 evidence 恢复守住 event-id 语义，并以 `0013a` 前滚旧 shape。Phase 13.6A 的独立 `ready-to-archive` 历史基线已由本轮联合代码 1+2 再次确认，作为 Phase 13.7 的只读基线，不在本轮重复开发。 |
| Provider usage 若由业务 agent 手工拼接，delegation budget 与 trace/eval 证据不可审计。 | model/embedding adapter、event/trace、parent aggregation。 | Phase 13.7、13.8 | 已缓解 | MOD-001 已建立并验证 durable provider-neutral evidence；DLG-001 只从持久化 child evidence 聚合，等待 Phase 13.8 同 digest 代码 1+2。 |
| 真实 delegation 容易造成循环、跨租户、预算放大、重复 child run 和 service crash 后双计费。 | registry、policy、runtime、RunQueue、storage、events。 | Phase 13.8 | 已缓解 | edge + policy + cycle/depth/budget 前置门禁、parent 级幂等 reservation、service crash/reclaim 与 durable aggregation 已由 local/真实 PostgreSQL/Redis 证据覆盖，等待代码 1+2。 |
| SSE 若复用 JSON route 或引入第二套事件状态，会造成 resume 漂移、visibility 泄漏和代理缓冲。 | Access、CanonicalEvent、OpenAPI、service deployment。 | Phase 13.9 | 已缓解 | RUN-006 与 CLI-EVT-001 复用统一授权 EventSink reader，Last-Event-ID 为唯一 SSE 续读输入，握手前/后错误分离，并已验证背压、真实 service resume 与首 frame P95。 |
