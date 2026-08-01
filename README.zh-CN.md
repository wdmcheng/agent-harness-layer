# Agent Harness Layer

[English](README.md) | [简体中文](README.zh-CN.md)

Agent Harness Layer 是一套用于构建企业后端 Agent 应用的 Python 核心包和可复制服务模板。它补齐普通演示项目经常拖到最后才处理的工程能力：类型化配置、身份与策略、可恢复运行与审批、受控工具、检索、可观测性、评测、打包和发布门禁。

这个仓库服务两类人：

- **Agent 应用开发者**：复制并扩展 [`templates/service-app`](templates/service-app/README.zh-CN.md)。
- **脚手架维护者**：维护可复用的 `agent_harness` 核心包、模板、adapter、契约和验证链路。

local profile 使用 SQLite、进程内队列、本地 JSONL 证据和 fake model，不需要真实模型 key 或外部可观测 SaaS。service profile 使用 PostgreSQL、Redis、migration、FastAPI，以及独立 runtime worker。

## 这个项目能做什么

当前仓库已经提供：

- 可构建的 `agent-harness` wheel 和 sdist；
- 可复制的 FastAPI、Typer、worker、Docker Compose 服务模板；
- profile、Agent、`.env`、进程环境变量和 secret file 的类型化配置；
- 租户身份、策略检查、输入 guardrail 与 HITL 审批；
- run、checkpoint、resume、幂等、队列和事件的持久化契约；
- 受 workspace、allowlist 和 policy 约束的文件、Shell 与 MCP 工具；
- local/PostgreSQL 检索，以及可选 PGroonga/pgvector adapter；
- local-first 事件、telemetry adapter 和 trace-to-eval 闭环；
- 四个可运行示例 Agent 与原子化 Agent 生成命令；
- quality、test、eval、smoke、build、license、CI contract 和 release preview 门禁。

它不是托管 Agent 平台，不提供产品化前端管理台，也不代表生产环境已经部署完成。GitHub/GitLab hosted runner、远端保护环境、外部 artifact service、真实 provider 集成和真实 registry 发布，在当前 checkout 中仍是 `hosted-unverified`。

## 先选你的入口

| 你要做什么 | 从哪里开始 |
|---|---|
| 第一次运行 Agent 应用 | [service-app 首次使用](templates/service-app/README.zh-CN.md#首次使用local-profile) |
| 创建业务 Agent | [创建 Agent](templates/service-app/README.zh-CN.md#创建-agent) |
| 让 AI / Agent 初始化项目或实现功能 | [AI / Agent 项目操作指南](templates/service-app/docs/ai-agent-guide.zh-CN.md) |
| 把五层两翼落实为一个 Agent | [五层两翼开发 Agent 指南](docs/building-an-agent.zh-CN.md) |
| 调用 HTTP API | [service-app HTTP API](templates/service-app/README.zh-CN.md#http-api) |
| 直接使用 Python 包 | [Python API](#python-api) |
| 理解架构和安全边界 | [模块设计思路](#模块设计思路)和[深度文档](#深度文档) |
| 维护可复用脚手架 | [开发者指南](#开发者指南)和完整[贡献指南](CONTRIBUTING.zh-CN.md) |

## 把项目任务交给 AI / Agent

复制模板自带普通、按需使用的 [AI / Agent 项目操作指南](templates/service-app/docs/ai-agent-guide.zh-CN.md)。把链接交给 AI，或直接复制下面这段；指南本身不会自动配置工具，也不会施加目录级指令：

```text
先阅读 templates/service-app/docs/ai-agent-guide.zh-CN.md，再检查当前项目并完成这个任务：
<任务和验收标准>。

遵守指南中的架构、安全、验证和交付约束。除非我单独授权具体动作，否则不要
commit、push、deploy、publish、使用生产凭据或调用真实 provider。
```

把 `templates/service-app` 复制成独立项目后，指南路径变为 `docs/ai-agent-guide.zh-CN.md`。

## 用五层两翼开发一个 Agent

每个 Agent 并不需要重新实现七套系统。模板已经提供接入层和运行时层的大部分能力；业务代码主要实现引擎层，只在需求出现时增加工具层和基础设施，并让 Eval 与 Observability 两翼贯穿整个生命周期。

| 架构区域 | Agent 开发者实际要做什么 |
|---|---|
| 接入层 | 复用模板 CLI/HTTP API、认证、类型化请求、OpenAPI 和 SSE |
| 运行时层 | 注册 `config.yaml`，实现 `AgentExecutor` 合同；复用 run、checkpoint、approval 和 delegation |
| 引擎层 | 定义 `schemas.py`、`agent.py`、模型选择、预算和业务行为 |
| 工具层 | 只增加 Agent 真正需要的类型化工具、allowlist、workspace policy 和 HITL 规则 |
| 基础设施层 | 先用 local/fake；按需配置 storage、queue、retrieval、provider 和 secret |
| 左翼 Eval Gate | 把人工确认的行为变成 approved 回归证据；自动信号只能停在 draft |
| 右翼 Observability | 先检查本地 event、usage 和 audit；外部 telemetry provider 只是可选 fan-out |

完整的请求流、最小实现步骤、复杂度选择和 `support.triage` 对照见[五层两翼开发 Agent 指南](docs/building-an-agent.zh-CN.md)。图中的 Graph Nodes/GraphState 和独立 gateway 是未来扩展位，不是首次开发前置；概念性 `@agent.tool` 标签在当前实现中对应公共 `ToolRegistry`，项目没有绕过 registry、policy 或 approval 的 decorator。

## 准备环境

本地开发必须准备：

- macOS 或 Linux；
- Git；
- GNU Make；
- Python `>=3.12`；
- 本地开发与 release wrapper 使用 uv `>=0.11.29,<0.12`；CI 当前具体选择 `0.11.29`，单次发布 artifact 记录实际使用的受支持 patch。

运行项目前先检查：

```bash
python3 --version
uv --version
git --version
make --version
```

如果 uv 未安装或不在支持范围内，可以使用 [uv 官方版本化安装方式](https://docs.astral.sh/uv/getting-started/installation/)安装当前 CI 的具体版本，也可以用包管理器切换版本：

```bash
curl -LsSf https://astral.sh/uv/0.11.29/install.sh | sh
uv --version
```

三份 `pyproject.toml` 对外部依赖声明有上下界的兼容范围。根项目和服务模板刻意保留 `agent-harness==0.1.0`，因为它们与本仓库核心包作为同一版本集合发布。`uv.lock` 仍保存受审的精确解析：普通 `uv sync --frozen` 与 `uv lock --check` 不会升级依赖；需要升级时单独执行 `uv lock --upgrade`，并审查 lock diff。

service profile 还需要 Docker 和 Compose v2，安装方法见 [Docker Compose 官方文档](https://docs.docker.com/compose/install/)。

## 第一次使用

### 1. 安装 workspace

在仓库根目录执行：

```bash
uv sync
```

#### PyCharm 与 Pyright

本仓库约定整个 uv workspace 统一使用仓库根目录的 `.venv`，成员项目不各自创建
环境。已提交的根 `[tool.pyright]` 因此明确配置 `venvPath = "."` 和
`venv = ".venv"`。当前 PyCharm 需要显式的 `venvPath` 和 `venv` 才能稳定解析
依赖；这里的具体值来自本仓库的根 `.venv` 约定。模板成员用
`venvPath = "../.."` 指向同一个根环境。模板复制成独立项目后，第一次 bootstrap
会把该值归一化为项目内的 `.`。
模板成员还会在自己的 `tool.uv.sources` 中重复声明
`agent-harness = { workspace = true }`，因为 PyCharm 可能不会把根 source 映射应用到
该成员。复制项目第一次 bootstrap 会在同步依赖前替换或删除这个 workspace-only 来源。

PyCharm 中启用 `Use pyproject.toml-based project model`，执行
`Sync Project with pyproject.toml`，并确认项目 SDK 指向根 `.venv`。

修改 TOML 后，仅同步 PyCharm 项目模型不一定会重启已经运行的 Pyright；从状态栏
语言服务菜单重启 Pyright，或重开项目，才能让新环境配置立即生效。

根 `make pyright` 和 `make quality` 会校验当前 uv 环境是否符合上述根 `.venv`
约定，避免两套环境造成误导性的假通过。

##### 覆盖根 `.venv` 约定

确需把本仓库的虚拟环境放到其他位置时，同时设置 uv 环境和仅供本机使用的
`pyrightconfig.json`。例如：

```bash
export UV_PROJECT_ENVIRONMENT="/absolute/path/to/python envs/agent-harness-layer"
```

在仓库根目录创建：

```json
{
  "extends": "./pyproject.toml",
  "venvPath": "/absolute/path/to/python envs",
  "venv": "agent-harness-layer"
}
```

然后执行 `uv sync` 和 `make quality`，并把 PyCharm 项目 SDK 指向同一个环境中的
Python，再同步项目模型并重启 Pyright。`pyrightconfig.json` 优先于已提交的
`[tool.pyright]`；Make 入口不会生成或改写它，根 `.gitignore` 也会默认忽略该文件。
不要在 Pyright 路径字段中使用 `~` 或环境变量，因为 Pyright 不展开它们。

复制出的独立项目请按
[模板中的自定义虚拟环境说明](templates/service-app/README.zh-CN.md#自定义虚拟环境路径)
操作；该说明会随模板一起复制。具体优先级和 uv 默认环境行为见官方
[Pyright 配置文档](https://github.com/microsoft/pyright/blob/main/docs/configuration.md)与
[uv 项目环境文档](https://docs.astral.sh/uv/concepts/projects/layout/)。

### 2. 验证离线开发链路

```bash
make quality
make test
make smoke-local
make eval
```

这些命令走 local/fake 路径，不需要真实模型 API key。`make smoke-local` 会创建隔离状态、注入临时 budget fingerprint key、迁移自己的数据库，并验证打包后的 CLI。

### 3. 启动真正可访问的服务模板

仓库检查不会留下长期运行的 API。要实际使用应用，请继续进入模板：

```bash
cd templates/service-app
make bootstrap

export AGENT_HARNESS_BUDGET__FINGERPRINT_KEY="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export STATE_DIR="$PWD/.agent-harness/local"
export STORAGE_DSN="sqlite+aiosqlite:///$STATE_DIR/agent_harness.db"
export AGENT_HARNESS_STORAGE__DSN="$STORAGE_DSN"

mkdir -p "$STATE_DIR"
uv run python app/migrate.py \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"

make dev
```

启动后访问：

- health：`http://127.0.0.1:8000/api/v1/health`
- Swagger UI：`http://127.0.0.1:8000/docs`
- Redoc：`http://127.0.0.1:8000/redoc`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`

同一个状态数据库的整个生命周期内必须使用稳定的 fingerprint key。它是预算请求指纹 secret，不是模型 API key；不要提交到版本库。

## 使用指南

### 受控非流式真实模型运行时

仓库提交的 local/service profile 都显式选择 `fake_default`；即使进程里存在
`OPENAI_*` 或代理环境变量，普通 quality、test、eval 和 local smoke 仍保持离线。真实文本
deployment 只能通过品牌化 `AGENT_HARNESS_MODEL__...` 类型化配置显式启用，并引用
credential、exact endpoint policy 和版本化 model catalog。Agent YAML 只能收窄
deployment/model allowlist，请求只能再次收窄；两者都不能选择 URL、credential、SDK
client 或冻结交集之外的模型。

运行时会在 policy、预算、client 和网络副作用前冻结 route。每个 root run 持久化版本化
路由快照，恢复时继续使用原 endpoint path、catalog、attempt 上界和价格 identity。异步
provider 路径受单一 total deadline、有界 retry、进程内按 deployment 隔离的 Bulkhead、
耐久 side-effect mark 和保守 `unknown` 语义约束；真实调用失败不会切换到 fake。

字段名与 secret-file 接法见 [service-app 环境示例](templates/service-app/.env.example)。
`make smoke-live-model` 在当前会话没有另行授权时保持惰性：输出
`model-live-smoke/v1 status=hosted-unverified`、`provider_called=false`，并以 0 退出。
已授权运行还必须显式 opt-in、提供隔离的品牌化 credential 与受信 endpoint，并完整经过
orchestrator、policy audit、shared budget reservation、usage evidence 和 provider lifecycle。
同一 deployment 可声明有序的多个 fallback models；router 只在 provider 副作用前按冻结
预算选择，真实失败后不自动重放。非默认 secret 目录必须通过 `--secret-root` 显式限定。

显式跨 deployment fallback 使用 Agent `fallback_routes` 冻结候选链。每个候选独立绑定
endpoint、credential、catalog、Bulkhead 和预算；只有耐久的 `client_not_started` 或受
endpoint policy约束的 `trusted_business_not_started` proof 才能推进。任一 unknown、未受信
response、usage、文本或首 delta 都会围栏后继。`make smoke-live-model-failover` 默认零调用并
报告 `hosted-unverified`；真实验证还需要五项显式授权前置和
`AGENT_HARNESS_LIVE_MODEL_FAILOVER_DEPLOYMENTS=<primary>,<secondary>`。

### 根目录常用命令

| 命令 | 证明什么 | 额外前置 |
|---|---|---|
| `make quality` | Ruff 格式/检查、Pyright、import boundary | 本地工具链 |
| `make test` | 单元、合同和离线集成行为 | 本地依赖集 |
| `make eval` | approved fake-model eval cases | 无需真实 provider key |
| `make smoke-local` | 隔离的 SQLite/in-memory/local-JSONL runtime | 无需外部服务 |
| `make smoke-live-model` | 如实报告 opt-in 状态，或执行一次受治理 completion | 需另行授权、品牌化隔离 credential 与受信 endpoint |
| `make smoke-live-model-failover` | 校验双 deployment failover artifact，或执行受控真实链 | 需另行授权、双隔离 credential/endpoint 与受信 not-started fixture |
| `make smoke-service` | 复制模板后经 PostgreSQL/Redis 验证 API/worker 恢复 | Docker Compose |
| `make build` | 本地 wheel、sdist 和 checksum | 不发布 |
| `make license-check` | 依赖/license 清单、NOTICE、vendoring 和镜像身份策略 | 不是法律意见或完整 SBOM |
| `make release-dry-run` | 被忽略的本地发布预览 | 不 tag、不 push、不发布 |

### 核心 CLI

核心命令是 `agent-harness`：

```text
agent-harness doctor
agent-harness agents list
agent-harness run <agent_id>
agent-harness events stream <run_id>
agent-harness tools list|call
agent-harness policy check
agent-harness approvals list|approve|deny
agent-harness eval draft|list|approve|run|scores|experiment
agent-harness scaffold agent <agent_id>
agent-harness migrate-local-state
```

模板只额外提供 `agent-harness-service serve`，不会复制核心业务命令。完整 local profile 用法见 [service-app CLI 指南](templates/service-app/README.zh-CN.md#cli)。

### HTTP API

service-app 在 `/api/v1` 暴露 agents、runs、JSON events、SSE events、approvals、policy check、eval cases、eval runs、eval experiments 和 health。local profile 最短调用链：

```bash
curl -sS http://127.0.0.1:8000/api/v1/agents

curl -sS \
  -H 'Content-Type: application/json' \
  -d '{"input": {}, "idempotency_key": "first-run"}' \
  http://127.0.0.1:8000/api/v1/agents/examples.basic/runs
```

从响应中取出 `run_id` 后读取详情和事件：

```bash
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID"
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events?after_seq=0"
curl -N \
  -H 'Accept: text/event-stream' \
  -H 'Last-Event-ID: 0' \
  "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events/stream"
```

local profile 使用默认本地身份。service profile 要求 `Authorization: Bearer <token>`，tenant/user 身份来自服务端 verifier，不能由请求 body 覆盖。字段、精确状态码、幂等、可见性与恢复语义以 [`API-Contract.md`](API-Contract.md) 为准。

## Python API

顶层 `agent_harness` 包刻意只导出 `__version__`。稳定能力从明确子模块导入，让依赖方向在代码里可见。

### 加载类型化配置

```python
from pathlib import Path

from agent_harness.config import load_settings

settings = load_settings(
    profile="local",
    profiles_dir=Path("templates/service-app/configs/profiles"),
)
print(settings.profile)
```

合并优先级是 profile YAML → agent YAML → `.env` → 受信 secret file → 进程环境变量 → 显式 overrides。非法配置会在 API、worker、migration 或 run 产生外部副作用之前失败。

### 发现 Agent

```python
from pathlib import Path

from agent_harness.registry import AgentRegistry

registry = AgentRegistry.load_from_directory(Path("templates/service-app/agents"))
for descriptor in registry.list_agents():
    print(descriptor.agent_id, descriptor.description)
```

`load_from_directory()` 会先完整校验 descriptor、schema reference 和 executor reference，再发布可用 registry。任何一项非法都会整体拒绝，不会静默跳过坏 Agent。

### 实现 executor

```python
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class ExampleExecutor:
    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del context
        return AgentExecutionResult.completed({"echo": request.input})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del request, context, grant
        return AgentExecutionResult.failed("this agent has no approval continuation")


executor = ExampleExecutor()
```

registry 会加载 `config.yaml` 中引用的模块级 `executor`。runtime 编排、身份、policy、checkpoint、event 和恢复仍在业务 executor 之外完成。

### 稳定 payload 和 helper

公共 DTO 继承 `HarnessDTO`；`to_payload()` 返回 JSON-compatible 字典并省略 `None` 字段：

```python
from agent_harness.contracts import HarnessDTO


class Output(HarnessDTO):
    result: str
    optional_ref: str | None = None


payload = Output(result="ok").to_payload()
assert payload == {"result": "ok"}
```

其他聚焦 helper 包括 `retrieval_result_to_context_fragment()`、`retrieval_results_to_context_fragments()`、`merge_rrf()`、`mcp_tools_from_client()`、`build_execute_message()` 和 `build_resume_approval_message()`。它们只做稳定边界归一化，不会绕过 policy、identity、trust、approval 或持久化门禁。

## 便捷封装和“语法糖”

项目确实提供便捷层，但刻意没有用一个魔法 decorator 或一行 DSL 把治理链路藏起来：

- `make ...` target 是仓库脚本与 CLI 的稳定短入口。
- `agent-harness scaffold agent support.triage` 会原子生成安全 Agent package、类型化 schema、config、空工具/delegation 权限和 draft eval case。
- `AgentRegistry.load_from_directory()` 省去手工枚举 YAML 和处理动态 import。
- `AgentExecutionResult.completed(...)`、`.waiting(...)`、`.failed(...)` 是互斥结果的类型化构造器。
- `HarnessDTO.to_payload()` 是公共边界的序列化快捷方法。
- `config.yaml` 是声明式注册语法：不用在 app 层手工接线，即可选择 schema、executor、model policy、budget、tool allowlist、eval dataset 和 delegation edge。
- CLI `--prompt` 是交互式 run 的输入便利项；业务 Agent 仍要做自己的类型校验，它不是绕过 schema 的通用通道。

所有便捷层最终都回到同一套 registry、runtime、policy、storage 和 event seam。任何跳过这些边界的“快捷方式”都不是支持的语法糖，而是缺陷。

## 目录结构

```text
agent-harness-layer/
├── packages/agent-harness/       # 可构建、provider-neutral 的核心包
├── templates/service-app/        # 可复制 FastAPI/CLI/worker 应用
│   └── agents/examples/          # 持续维护的可运行示例
├── examples/                     # 预留的包级示例区域
├── docs/                         # 架构、扩展、安全、eval、release、ADR
├── scripts/                      # quality、smoke、build、合规和发布工具
├── tests/                        # 仓库合同与集成证据
├── compliance/                   # 依赖/license 策略与观察记录
├── openspec/                     # 长期行为规格和已归档变更
├── Product-Spec.md               # 产品级真相源
├── API-Contract.md               # 字段级 API/CLI/module 合同
├── DEV-PLAN.md                   # 分阶段实现和证据计划
├── pyproject.toml                # uv workspace 与工具链 pin
├── Makefile                      # 本地/CI 稳定入口
├── LICENSE
├── NOTICE
├── README.md                     # 英文入口
└── README.zh-CN.md               # 中文入口
```

根 `examples/` 只是预留目录，不是模板可运行示例的位置。当前维护的示例在 `templates/service-app/agents/examples/`。

## 模块设计思路

| 模块 | 设计意图 |
|---|---|
| `contracts`、`identity`、`config` | 提供稳定校验数据、trust marker、结构化错误和 fail-closed 启动输入。 |
| `registry`、`runtime`、`delegation` | 发现 executor，协调 run、checkpoint、queue、approval continuation 和父子生命周期，不让业务代码走捷径。 |
| `policy`、`approvals`、`auth`、`audit` | 分离认证、授权、人工审核和证据，同时保持 tenant/run/request/trace 关联。 |
| `tools`、`mcp`、`artifacts` | 在副作用或上下文注入前完成名称/schema 校验、allowlist、workspace、policy、脱敏和大结果外置。 |
| `models`、`embeddings`、`retrieval`、`context` | 隔离 provider，保留 usage/budget/source/trust 证据，并组装有界上下文。 |
| `events`、`observability`、`evals` | 先提交本地证据，再做可选 provider fan-out；draft 必须经人工审核才能进入 approved eval。 |
| `storage`、`adapters` | 持有 SQLAlchemy repository/UoW/migration，并把 vendor SDK/driver import 隔离在核心合同和业务 Agent 之外。 |
| `templates/service-app/app` | 薄 HTTP/CLI/worker 组合与 DTO 转换，不写业务 Agent 逻辑。 |
| `templates/service-app/agents` | 存放业务 executor、schema、config、tools 和 Agent 专属 eval case。 |

service profile 现在已物理拆分 API 与 runtime worker。model/tool gateway、event pipeline 和 storage service 是未来拆分点；当前 provider 和 repository seam 仍在进程内。

## 开发者指南

人和 Agent 的完整工作流、文件所有权、机械规则真相源、验证矩阵与 Git/安全边界，见
[贡献指南](CONTRIBUTING.zh-CN.md)。

不要把本 README 当成第二份工作流规范。事实源顺序（包括适用的已同步 OpenSpec 主规格和 active
delta）、最小充分验证及授权边界统一以贡献指南为准。

必须保持这些依赖规则：

- `agent_harness/*` 不依赖模板或具体示例 Agent。
- `app/*` 只负责协议入口和装配，不写业务 Agent 逻辑。
- `agents/*` 只使用 `agent_harness` 公共 seam，不直接 import vendor SDK 或 ORM session。
- vendor SDK 只留在批准的 adapter/integration boundary。
- `eval-cases/approved` 只能由审核流程写入。
- delegation 必须经过 `AgentRegistry`、`PolicyEngine` 和 runtime service。
- 所有运行记录及其证据必须保留 `tenant_id`、`agent_id`、`run_id`；request/trace ID 用于追加关联，不能替代这三个所有权键。

验证命令按 `CONTRIBUTING.zh-CN.md` 的权威矩阵选择；本 README 不再维护第二套固定命令组合。
只报告实际执行的检查及其能证明的边界。

## 贡献指南

人和 Agent 共同遵守的权威贡献流程见 [`CONTRIBUTING.zh-CN.md`](CONTRIBUTING.zh-CN.md)，
另有 [English edition](CONTRIBUTING.md)。

1. 从聚焦的问题或 change contract 开始，不混入无关清理。
2. 行为变化前先更新相关 Product/API/OpenSpec 契约。
3. 通过公开 seam 增加或修改测试：CLI、HTTP、module protocol、repository/UoW、event 或持久化边界。
4. 保持 package 与 vendor import boundary。
5. 使用 `feat:`、`fix:`、`docs:`、`refactor:`、`chore:` 等 Conventional Commit 前缀。
6. 运行相关验证命令，并在审查说明中写出精确结果。
7. 不提交 `.env`、`.agent-harness`、数据库、trace、credential 或生成的 release preview。

仅修改文档也要核对命令、内部链接、中英文事实一致性，以及“当前能力/未来能力”表述。

## 常见问题排查

### uv 拒绝所有命令

如果错误提示 uv 版本与 required version 不匹配，请选择 `>=0.11.29,<0.12`；复现当前 CI 环境时使用 `0.11.29`。preview、正式 build 与 publish plan 会记录各自实际使用的 uv patch。这个拒绝发生在项目代码运行之前。

### `config.invalid` 或 fingerprint key 缺失

直接运行 `doctor`、`agents list`、`run`、API、worker 或 migration 前，导出 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY`。同一个状态库要保持值稳定，但不要提交它。

### `storage.migration_required`

对即将使用的同一个 `STORAGE_DSN` 执行模板 migration。只改环境变量而不迁移对应数据库，不会完成初始化。

### health 正常，但 run 不推进

local profile 先检查 run events 和本地 JSONL。service profile 检查 migration、Redis reachability/consumer group、worker readiness 和 PostgreSQL 状态。health 只是配置/能力摘要，不是端到端 run 证明。

### registry 看不到 Agent

检查点分小写 `agent_id`、`config.yaml`、schema reference、模块级 executor reference，以及当前 `--agents-dir`。descriptor 非法或重复时，registry 会整体失败。

### 工具被拒绝

检查 Agent tool allowlist、workspace 规范化与 `.agentignore`、identity permission、policy decision 和 approval 状态。不要为了通过而扩大 workspace root 或绕过 `ToolRegistry`。

## 安全说明

- 不要把 secret 写进 profile YAML、README 示例、请求 body、trace/eval/audit payload 或提交的 `.env`。
- 部署 secret 走进程环境变量或受信 `_FILE` 配置边界。
- user、retrieval、MCP 和 tool 内容进入上下文前都按 untrusted 处理。
- 原始 resume token 不是审批权限；approval-gated continuation 必须经过 `ApprovalService`。
- 除非未来契约明确新增，否则不要暴露远程 `/api/v1/tools` route。

更多边界见[安全策略](docs/security-policy.zh-CN.md)与[上下文和信任边界](docs/context-and-trust-boundary.zh-CN.md)。

## 深度文档

| 你要回答的问题 | 文档 |
|---|---|
| 人和 Agent 应如何规划、隔离、验证并交付一次变更？ | [贡献指南](CONTRIBUTING.zh-CN.md) |
| 架构变更遵守哪些分层、设计原则和模式选择规则？ | [工程原则](docs/engineering-principles.zh-CN.md) |
| 如何用五层两翼做出一个可运行 Agent？ | [五层两翼开发 Agent 指南](docs/building-an-agent.zh-CN.md) |
| 本项目与 Pydantic AI Harness、Agently 是什么关系？ | [框架定位与能力对照说明](docs/framework-positioning.zh-CN.md) |
| 如何把项目初始化或功能实现交给 AI / Agent？ | [AI / Agent 项目操作指南](templates/service-app/docs/ai-agent-guide.zh-CN.md) |
| 今天实际运行什么，未来可能拆什么？ | [架构与部署边界](docs/architecture/README.zh-CN.md) |
| 可以在哪里增加 Agent 或能力？ | [扩展指南](docs/extension-guide.zh-CN.md) |
| 哪些 DTO/protocol/facade/repository/UoW 边界要稳定？ | [Adapter 合同](docs/adapter-contracts.zh-CN.md) |
| 不可信输入如何组装和治理？ | [Context 与信任边界](docs/context-and-trust-boundary.zh-CN.md) |
| 身份、policy、审批、secret、audit 如何协作？ | [安全策略](docs/security-policy.zh-CN.md) |
| trace 如何变成经审核的 eval 证据？ | [Eval 与 Observability 闭环](docs/eval-observability-loop.zh-CN.md) |
| 什么能在本地证明，什么仍是 hosted-unverified？ | [发布流程](docs/release-process.zh-CN.md) |
| 为什么选择这些服务、vendor 和 Redis 边界？ | [ADR](docs/adr/0001-p0-service-boundaries.zh-CN.md) |

## 许可证和发布边界

项目使用 Apache-2.0，见 [`LICENSE`](LICENSE) 和 [`NOTICE`](NOTICE)。运行时依赖策略与观察记录位于 `compliance/`；新增或升级依赖、运行镜像前运行 `make license-check`。

`make build` 只生成本地 wheel/sdist 和 checksum；`make release-dry-run` 只生成被忽略的本地预览。两者都不会 publish、push、tag、deploy，也不能证明 hosted CI。任何受保护 promotion 或私有 registry 执行前，先阅读[发布流程](docs/release-process.zh-CN.md)。
