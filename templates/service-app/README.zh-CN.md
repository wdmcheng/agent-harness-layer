# Agent Harness Service App 模板

[English](README.md) | [简体中文](README.zh-CN.md)

这个目录是基于 `agent-harness` 核心包构建的可复制后端应用模板。它把 FastAPI、薄 service CLI、runtime worker、类型化 local/service profile、Docker Compose、示例 Agent、eval 数据和公开 seam 测试组装在一起，同时保持业务 Agent 逻辑与应用入口层分离。

如果你要开发 Agent 服务，从这个模板开始；只有在准备修改可复用核心包或模板本身时，才从仓库根目录开始。

## 让 AI / Agent 操作项目

模板自带普通、按需使用的 [AI / Agent 项目操作指南](docs/ai-agent-guide.zh-CN.md)，它不是自动生效的指令文件。需要 AI 初始化复制项目或实现功能时，可以把链接交给它，或者直接复制下面这段：

```text
先阅读 docs/ai-agent-guide.zh-CN.md，再检查当前项目并完成这个任务：
<任务和验收标准>

遵守指南中的架构、安全、验证和交付约束。除非我单独授权具体动作，否则不要
commit、push、deploy、publish、使用生产凭据或调用真实 provider。
```

指南末尾还提供了更完整的“初始化项目”和“实现功能”提示词模板。

## 模板提供什么

- `local` profile：SQLite、进程内队列、本地 JSONL 证据、fake model，无需外部 provider key；
- `service` profile：PostgreSQL、Redis、migration、FastAPI API 和独立 runtime worker；
- `/api/v1` HTTP 管理面，以及 OpenAPI、Swagger、Redoc；
- 核心 `agent-harness` CLI：agents、runs、events、tools、policy、approvals、eval、scaffold；
- app-specific `agent-harness-service serve` 命令；
- 四个可运行示例：RAG assistant、ticket triage、repo analyst、dev assistant；
- 一个确定性的 `examples.basic` 冒烟夹具；
- 带安全默认值和 draft eval case 的原子 Agent 生成器；
- 复制项目可直接使用的 quality、test、eval、local smoke 和真实 service smoke 命令。

service profile 已把 API 和 worker 拆成独立进程。model/tool gateway、event pipeline 和 storage service 是未来边界，不是当前 Compose 服务。

## 准备环境

本地使用需要：

- macOS 或 Linux；
- Python `>=3.12`；
- Git 和 GNU Make；
- 在源仓库中使用**精确版本 uv `0.11.29`**；
- 复制模板独立使用时，准备受信的 `agent-harness` wheel、sdist、源码目录或私有 index。

先检查工具链：

```bash
python3 --version
uv --version
git --version
make --version
```

`make smoke-service` 还需要 Docker 和 Compose v2。默认 local/service profile 都使用 fake model，除非你主动配置其他 provider，否则不需要真实模型 API key。

## 首次使用：local profile

### 1. 选择核心包来源

在源仓库 workspace 内：

```bash
cd templates/service-app
make bootstrap
```

把当前目录复制成独立项目后，第一次 bootstrap 必须提供受信 artifact 或源码路径：

```bash
make bootstrap \
  AGENT_HARNESS_SOURCE=/absolute/path/to/agent_harness-0.1.0-py3-none-any.whl
```

`bootstrap` 会把受信本地来源写入复制项目自己的 `tool.uv.sources`，后续命令可直接复用。如果组织已经把 `agent-harness==0.1.0` 发布到受信私有 index，配置 `UV_INDEX_URL` 后显式允许 index：

```bash
make bootstrap AGENT_HARNESS_ALLOW_INDEX=1
```

独立模板默认不会解析公共同名包。这是供应链边界，不是安装故障。

### 2. 创建本地状态并迁移

为当前状态库生成 fingerprint key，导出数据库位置，然后执行 migration：

```bash
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
```

fingerprint key 是预算请求身份 secret，不是模型 API key。同一个状态数据库整个生命周期要保持稳定；不要把值写进 `local.yaml`、文档或 Git。

如果希望持久保存本机覆盖项，可以把 `.env.example` 复制为已忽略的 `.env`，但必须填当前环境自己的 key，不能提交默认值：

```bash
cp .env.example .env
```

### 3. 验证 profile 并运行第一个 Agent

```bash
make smoke-local
make run-basic
```

`make smoke-local` 验证配置和 registry 发现；`make run-basic` 会实际经过 registry/runtime/event 链路，并打印 `run_id`、status 和 terminal event。

### 4. 启动 API

```bash
make dev
```

等价的显式命令是：

```bash
uv run agent-harness-service serve \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --host 127.0.0.1 \
  --port 8000
```

启动后访问：

- health：`http://127.0.0.1:8000/api/v1/health`
- Swagger UI：`http://127.0.0.1:8000/docs`
- Redoc：`http://127.0.0.1:8000/redoc`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`

## 日常使用

### Make 命令

| 命令 | 用途 |
|---|---|
| `make bootstrap` | 解析受信核心包来源并同步依赖 |
| `make dev` | 用当前 profile 启动 FastAPI |
| `make cli ARGS='<核心命令>'` | 调用核心 CLI，不复制命令逻辑 |
| `make run-basic` | 执行确定性冒烟 Agent |
| `make run-rag` | 运行 RAG assistant 示例 |
| `make run-ticket` | 运行 ticket triage 示例 |
| `make run-repo` | 运行 repo analyst 示例 |
| `make run-dev` | 运行 dev assistant 示例 |
| `make test` / `make contract` | 运行复制模板后的公开 seam 测试 |
| `make quality` | 对 app、agents、tests、scripts 执行 Ruff 和 Pyright |
| `make eval` | 运行全部 approved 示例 eval case |
| `make eval-rag|eval-ticket|eval-repo|eval-dev` | 只运行一个示例的 eval |
| `make smoke-local` | 验证 local profile 和 Agent registry |
| `make smoke-service` | 运行真实 PostgreSQL/Redis/API/worker service smoke |
| `make worker` | 使用当前 profile 启动 runtime worker |

所有 target 都支持通过 Make 变量覆盖 `PROFILE`、`PROFILES_DIR`、`STATE_DIR`、`STORAGE_DSN`、`EVENTS_PATH`、`HOST`、`PORT`。

## CLI

模板 CLI 只拥有 `serve`。其他管理操作都来自核心 CLI，因此 HTTP、CLI 和 worker 共用同一 DTO、service、错误与授权语义。

### 检查配置

```bash
uv run agent-harness doctor \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"
```

### 列出和运行 Agent

```bash
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness run examples.basic \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --idempotency-key first-cli-run
```

在复制后的 service-app 中，`run` 和 `agents list` 必须像上面的示例一样显式传 `--agents-dir ./agents`。它们当前的默认值仍是源 workspace 路径 `templates/service-app/agents`，不会自动发现复制项目；`make run-*` target 会显式传入应用 agents 目录。只有 `scaffold agent` 在省略 `--agents-dir` 时，才会根据复制项目的 `pyproject.toml` 标记发现 `./agents`。

### 读取事件流

从真实输出取得 run ID，再读取 canonical NDJSON：

```bash
RUN_OUTPUT="$(make run-basic)"
printf '%s\n' "$RUN_OUTPUT"
export RUN_ID="$(printf '%s\n' "$RUN_OUTPUT" | awk '/^run_id:/ {print $2; exit}')"
test -n "$RUN_ID"

uv run agent-harness events stream "$RUN_ID" \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --events-path "$STATE_DIR/traces.jsonl"
```

CLI `--after-seq` 是 exclusive cursor；HTTP SSE route 使用唯一的 `Last-Event-ID` header。两者默认只读 public event，并在 terminal event 后结束。

### 检查策略

```bash
uv run agent-harness policy check \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --action run.read \
  --resource run
```

tools、approvals、eval、experiment、local-state migration 的精确参数，以 `agent-harness --help` 和 `<group> --help` 为准。

## HTTP API

local profile 使用默认开发身份。service profile 要求 `Authorization: Bearer <token>`，并由服务端 verifier 生成 tenant/user/permissions。

### 常用 route

| 方法和路径 | 用途 |
|---|---|
| `GET /api/v1/health` | 公开 liveness/配置能力摘要 |
| `GET /api/v1/agents` | 列出当前可见 Agent descriptor |
| `POST /api/v1/agents/{agent_id}/runs` | 创建 run |
| `GET /api/v1/runs/{run_id}` | 读取 durable run detail |
| `POST /api/v1/runs/{run_id}/cancel` | 取消非终态 run |
| `POST /api/v1/runs/{run_id}/resume` | 恢复普通 checkpoint；不能绕过审批 |
| `GET /api/v1/runs/{run_id}/events` | 用 `after_seq` 读取 JSON events |
| `GET /api/v1/runs/{run_id}/events/stream` | 用 `Last-Event-ID` 读取 SSE |
| `GET /api/v1/runs/{run_id}/approvals` | 列出 run approvals |
| `GET /api/v1/runs/{run_id}/approvals/{approval_id}` | 读取单项 approval |
| `POST /api/v1/runs/{run_id}/approvals/{approval_id}` | approve/deny waiting action |
| `POST /api/v1/policies/check` | 检查 policy action/resource/context |
| `/api/v1/eval-cases/*` | 创建、列出、批准 eval case |
| `/api/v1/evals/runs/*` | 运行 approved eval 并读取 score |
| `/api/v1/evals/experiments/*` | 创建、比较、接受 experiment |

项目刻意不提供远程 `/api/v1/tools` endpoint。工具执行只允许走 CLI/runtime `ToolRegistry` seam。

### 创建并检查 run

```bash
curl -sS http://127.0.0.1:8000/api/v1/agents

RUN_JSON="$(curl -sS \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: readme-first-run' \
  -H 'X-Trace-Id: readme.first.run' \
  -d '{"input": {}, "idempotency_key": "readme-first-run"}' \
  http://127.0.0.1:8000/api/v1/agents/examples.basic/runs)"
printf '%s\n' "$RUN_JSON"

export RUN_ID="$(RUN_JSON="$RUN_JSON" uv run python -c \
  'import json, os; print(json.loads(os.environ["RUN_JSON"])["run_id"])')"

curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID"
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events?after_seq=0"
```

### 用 SSE 读取

```bash
curl -N \
  -H 'Accept: text/event-stream' \
  -H 'Last-Event-ID: 0' \
  "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events/stream"
```

cursor 是 exclusive。terminal event 会关闭 stream；`include_internal=true` 需要额外 policy 权限。

### 检查 policy decision

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -d '{"action": "run.read", "resource": "run", "context": {}}' \
  http://127.0.0.1:8000/api/v1/policies/check
```

service profile 还要加 `-H "Authorization: Bearer $SERVICE_TOKEN"`。不要在 body 中传 tenant、reviewer 或 permission identity。

日常探索用实时 Swagger/Redoc。源仓库内的字段、状态码、幂等、审批、事件可见性和恢复规则，以 [`../../API-Contract.md`](../../API-Contract.md) 为单一真相源。

## 创建 Agent

### 生成 package

在 service-app 根目录执行：

```bash
uv run agent-harness scaffold agent support.triage
```

它会生成 `agents/support/triage/`：

```text
support/triage/
├── __init__.py
├── agent.py
├── tools.py
├── schemas.py
├── config.yaml
└── evals/
    ├── drafts/example.yaml
    └── approved/
```

生成内容默认使用 fake model、空 tool allowlist、空 delegation edge、类型化 executor 和仅 draft 的 eval。没有 `--force`；已存在目标、非法 ID、路径穿越和 symlink 越界都会在发布前被拒绝。

### 实现 executor

Agent 在模块级暴露一个满足公共 runtime protocol 的 executor：

```python
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class SupportTriageExecutor:
    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del context
        return AgentExecutionResult.completed(
            {"category": "unknown", "input": request.input, "needs_review": True}
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del request, context, grant
        return AgentExecutionResult.failed("no approval continuation is defined")


executor = SupportTriageExecutor()
```

在 `schemas.py` 定义校验后的输入输出 DTO，在 `config.yaml` 完成声明式注册：

```yaml
agent_id: support.triage
version: 0.1.0
name: Support Triage Agent
description: Classifies support requests for human routing.
input_schema: agents.support.triage.schemas.SupportInput
output_schema: agents.support.triage.schemas.SupportOutput
executor: agent:executor
model:
  provider: fake
  default_model: fake-scaffold
  fallback_models: []
budget:
  max_tokens_per_run: 1024
  max_cost_usd_per_run: null
tool_allowlist: []
eval_dataset: agents/support/triage/evals/approved
delegation_edges: []
```

然后验证发现和执行：

```bash
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness run support.triage \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --prompt 'login stopped working'
```

生成的 draft 必须人工审核后才能进入 approved eval。scaffold 不会自动批准 case。

## 把 Agent 对应到五层两翼

使用模板意味着不必自己重写每个架构区域：

| 区域 | 在复制应用中要做什么 |
|---|---|
| 接入层 | 通常复用 `app/api`、核心 CLI、认证、OpenAPI 和 SSE；只有真实应用协议需要时才加 route |
| 运行时层 | 在 `config.yaml` 声明 Agent 并返回 `AgentExecutionResult`；run、checkpoint、approval 和 delegation 交给 registry/runtime |
| 引擎层 | 在 Agent package 实现类型化 schema 和 executor；通过配置选择模型与预算，不直接 import vendor SDK |
| 工具层 | 默认保持空 allowlist；确有需要时才注册最小类型化工具，并补 workspace、policy 和 HITL 边界 |
| 基础设施层 | 先用 local profile；按需增加 retrieval、provider、PostgreSQL/Redis 或业务 adapter |
| 左翼 Eval Gate | 审核生成的 draft 后才能进入 `approved`，用 approved case 建立回归证据 |
| 右翼 Observability | 先读取本地 run event、usage 和 audit；provider telemetry 只是可选 fan-out |

正常链路是 `CLI/HTTP -> 接入层 -> 运行时层 -> 引擎层 <-> 工具层 -> 基础设施层`；Eval 对同一行为做回归，Observability 记录每个阶段。产品架构图里的 Graph Nodes/GraphState 和独立 gateway 是未来扩展位，不是首次开发前置；图中的概念性 `@agent.tool` 标签对应公共 `ToolRegistry`，不是 decorator 捷径。

源仓库提供完整的[五层两翼开发 Agent 指南](../../docs/building-an-agent.zh-CN.md)。复制成独立项目后，这个仓库级链接不会随模板存在，因此本表保留了必要映射。本地的 [AI / Agent 项目操作指南](docs/ai-agent-guide.zh-CN.md) 会随复制保留，并告诉 AI 如何遵守这些边界。

## Python 组合 API

应用通常通过 CLI 和 HTTP 使用。需要嵌入核心包时，从明确的公共模块导入：

```python
from pathlib import Path

from agent_harness.config import load_settings
from agent_harness.registry import AgentRegistry

settings = load_settings(
    profile="local",
    profiles_dir=Path("configs/profiles"),
)
registry = AgentRegistry.load_from_directory(Path("agents"))

print(settings.profile)
print([item.agent_id for item in registry.list_agents()])
```

模板应用工厂也支持 route 和 health 测试注入。同时传入 `orchestrator` 与 `event_sink` 可以避免装配 storage；测试实际调用哪个 endpoint，仍要为它提供真实实现或专用 test double：

```python
from pathlib import Path
from typing import Any, cast

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from app.main import create_app

app = create_app(
    orchestrator=cast(Any, object()),
    event_sink=LocalJsonlEventSink(Path(".agent-harness/test-events.jsonl")),
    registry=AgentRegistry.load_from_directory(Path("agents")),
    approval_service=cast(Any, object()),
    eval_service=cast(Any, object()),
    profile="local",
    profiles_dir=Path("configs/profiles"),
)

assert "/api/v1/health" in app.openapi()["paths"]
```

这组最小注入只适用于 route shape 或 health 测试；要调用其他 endpoint，必须使用真实 `RunOrchestrator` 和对应服务依赖。生产启动仍应使用 `agent-harness-service serve` 或等价受控进程入口，让 migration 和配置错误在监听端口前失败。

## 便捷封装和“语法糖”

模板提供了明确的便利层：

- **Make target**：缩短可信、可重复的 CLI/script 调用。
- **Scaffold 项目根发现**：`scaffold agent` 可以从复制项目标记定位 `./agents`；`run` 和 `agents list` 仍需显式传 `--agents-dir ./agents`。
- **Agent scaffold**：先在隔离 staging 生成并校验完整 package，再一次原子 rename 发布。
- **声明式 `config.yaml`**：无需在 app 层手工接线，即可注册 schema、executor、model、budget、tools、eval 数据和 delegation。
- **`AgentExecutionResult.completed/waiting/failed`**：不用手工组合状态字段，就能构造一种合法结果。
- **`HarnessDTO.to_payload()`**：把稳定边界对象序列化为 JSON-compatible payload。
- **示例 prompt adapter**：把方便的 `--prompt` 文本翻译为各示例的类型化输入。

同样重要的是模板没有什么：没有绕过 registry 校验的 decorator，没有直接调用 tool callable 的捷径，不会自动批准 eval，也不会让 provider/ORM 原始对象穿过公共边界。

## 目录结构

```text
service-app/
├── app/
│   ├── api/                 # route、请求/响应 DTO、dependency、SSE
│   ├── cli/                 # 只提供 app-specific serve
│   ├── workers/             # 独立 runtime worker 入口
│   ├── main.py              # FastAPI factory、错误和生命周期装配
│   ├── runtime.py           # 公共核心 seam 的 composition root
│   └── migrate.py           # 受控 migration 入口
├── agents/
│   └── examples/            # basic、RAG、ticket、repo、dev 示例
├── configs/
│   ├── profiles/            # 类型化 local/service profile
│   └── policy/              # 默认 YAML policy
├── eval-cases/
│   ├── drafts/              # review queue，不作为 approved 评分
│   └── approved/            # 人工审核的应用数据集
├── tests/                   # 复制模板后的公开 seam 测试
├── docs/
│   ├── README.md / README.zh-CN.md
│   ├── ai-agent-guide.md / ai-agent-guide.zh-CN.md
│   ├── examples.md / examples.zh-CN.md
│   └── ...                  # 应用级运维指南
├── scripts/                 # bootstrap、eval、service smoke、admin helper
├── Dockerfile               # API/worker 共用 wheel-only 镜像
├── docker-compose.yml
├── .env.example
├── .gitignore
├── Makefile
├── pyproject.toml
├── README.md
└── README.zh-CN.md
```

## 模块设计思路

| 区域 | 职责与边界 |
|---|---|
| `app/main.py` | 创建唯一 FastAPI app，安装 router/错误映射，注入 dependency，负责组件关闭 |
| `app/runtime.py` | 通过核心公共 seam 绑定 settings、storage、registry、policy、events、queue、approvals、eval、delegation |
| `app/api` | 认证、校验、DTO 转换和 service 调用；不持有 ORM session，不写业务 Agent 逻辑 |
| `app/cli` | 只提供 `serve`，其余全部复用核心 CLI |
| `app/workers` | 消费 durable queue message，并从 PostgreSQL 真相恢复执行上下文 |
| `agents/<agent>` | 持有业务 executor、schema、允许的工具、config 和 Agent 专属 eval case |
| `configs/profiles` | 描述环境拓扑与 provider 选择，不保存已提交的 secret 值 |
| `configs/policy` | 声明 action 的 allow、deny 或 require approval |
| `eval-cases` | 分离 draft 与人工 approved 证据 |
| `scripts` | 让复制项目可以 bootstrap 和端到端验证，不依赖源仓库路径 |

vendor SDK 只能放在 `agent_harness` adapter 或明确批准的 integration boundary。模板 app 和业务 Agent 只依赖 provider-neutral DTO、protocol、facade、repository 与 UoW seam。

所有运行记录及其证据必须保留 `tenant_id`、`agent_id`、`run_id`；request/trace ID 用于追加关联，不能替代这三个所有权键。

## 配置设计

配置合并顺序：

```text
profile YAML
  → agent YAML
  → .env
  → 受信 *_FILE secret
  → 进程环境变量
  → 显式 override
```

环境变量用双下划线表示嵌套字段：

```bash
export AGENT_HARNESS_STORAGE__DSN="$STORAGE_DSN"
export AGENT_HARNESS_MODEL__PROVIDER=fake
export AGENT_HARNESS_SERVICE__API_PROCESS__ENABLED=false
```

同一字段的 direct value 与 `_FILE` 互斥。secret file 必须是受信根目录内的绝对路径、普通文件且不是 symlink。配置错误会结构化失败，并发生在外部连接或应用启动之前。

## 示例 Agent

| Agent | 展示什么 | 默认安全行为 |
|---|---|---|
| `examples.rag_assistant` | 本地检索、citation、保留 trust 的 context、fake model | 无来源时诚实返回 `no_source` |
| `examples.ticket_triage` | 类型化分类和 confidence | 模糊输入返回 `unknown` / `needs_review` |
| `examples.repo_analyst` | 通过 `ToolRegistry` 读取/搜索/列出 workspace 文件 | 无 shell；越界拒绝；长结果走 `artifact_ref` |
| `examples.dev_assistant` | 受控 file/shell、policy、HITL continuation | 危险动作等待审批；deny 对目标零副作用 |

命令、输入、预期输出和 eval 边界见 [`docs/examples.zh-CN.md`](docs/examples.zh-CN.md)。

## 开发和测试

应用级改动运行：

```bash
make quality
make test
make eval
make smoke-local
```

改动影响 migration、PostgreSQL、Redis、DBOS、service 认证、API/worker 拆分、queue 恢复、approval continuation 或共享 event/checkpoint 证据时，再运行 `make smoke-service`。

在源仓库内还要运行根门禁，因为它们会检查核心/模板 import boundary 和完整合同：

```bash
cd ../..
make quality
make test
```

新增或修改 endpoint 前先更新 `API-Contract.md`，并增加运行时 OpenAPI drift test。Swagger 能打开不等于字段合同已经证明。

## 贡献指南

对于复制后的业务应用：

1. 业务行为放在 `agents/*`，应用装配放在 `app/*`。
2. 每个行为和失败路径都提供类型化 schema 与公开 seam 测试。
3. 保持 `eval-cases/drafts` 与 `eval-cases/approved` 分离；升级为 approved 前必须人工审核。
4. 把部署/provider 选择记录在自己的 `docs/` 与 ADR。
5. 不提交 `.env`、`.agent-harness`、数据库、trace、token 或 provider payload。

对于上游模板贡献：

1. 必须在真实 copy-out 后证明可运行，不能依赖源仓库路径或根 `PYTHONPATH`。
2. 不增加成员级 `workspace = true` 或固定 `cd ../..` 假设。
3. `agent-harness-service` 只保留 `serve`，管理逻辑归核心 CLI。
4. 改动命令或行为时保持中英文 README 事实一致。
5. 运行根 quality/test 和相关 local/service smoke。

## Service profile

完整真实依赖验证：

```bash
make smoke-service
```

脚本会构建或消费核心 wheel，把模板复制到 workspace 外，只使用复制项目启动 PostgreSQL、Redis、migration、API 和 worker。它验证认证 HTTP enqueue、worker pickup/reclaim、DBOS 恢复、共享 PostgreSQL checkpoint/event evidence、SSE resume、approval continuation、deny 零 continuation 和范围化清理。

这个命令是验证 harness，不是生产部署方案。默认会删除本轮 container、network、volume、临时 credential、queue namespace 和复制 workspace。

如需只保留命名 PostgreSQL volume 排障：

```bash
SERVICE_APP_KEEP_DATA=1 make smoke-service
```

脚本仍会删除 container、network、临时 credential、Redis namespace 和 workspace 文件，并输出精确的 `docker volume rm` 命令。

## 常见问题排查

### uv required version 不匹配

源 workspace 只接受精确 `uv 0.11.29`。先切换版本，再诊断项目代码。

### 复制项目无法解析 `agent-harness`

运行 `make bootstrap AGENT_HARNESS_SOURCE=/absolute/path/to/wheel-or-source`。除非组织确实发布该包并显式启用 `AGENT_HARNESS_ALLOW_INDEX=1`，不要增加公共 index fallback。

### fingerprint key 缺失

在 doctor、migration、API、worker 或 run 装配前导出 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY`，或配置对应受信 `_FILE`。不要为已有状态库随意轮换。

### 需要 migration

用进程实际使用的同一 profile、profiles directory 和 `STORAGE_DSN` 运行 `app/migrate.py`。迁移另一个路径的数据库不算完成。

### Agent 没有出现在列表

检查点分小写 `agent_id`、schema reference、`executor: agent:executor`、package `__init__.py` 和解析出的 agents root。registry 会把重复或非法 sibling 作为整体错误拒绝。

### API 启动了，但 run 失败

先读取 run detail 和 events，再检查 policy、input guardrail、当前 profile、storage migration 和 executor 输出。service profile 还要检查 Redis、worker readiness 和 PostgreSQL。

### tool 或文件访问被拒绝

检查 Agent allowlist、`WorkspacePolicy`、`.agentignore`、shell allow/deny list、当前 identity、policy decision 和 approval record。不要直接调用文件系统或 subprocess 绕过结果。

### 8000 端口占用

覆盖 Make 变量：

```bash
make dev PORT=8010
```

## 安全边界

- user、retrieval、MCP 和 tool 内容都是 untrusted input。
- service identity 来自 Bearer/API-key verifier，不来自 body tenant 字段。
- 工具调用必须经过 schema、allowlist、workspace policy、`PolicyEngine`、脱敏、audit 和 artifact 处理。
- 原始 resume token 不能批准危险动作，必须走 approvals CLI/API。
- 本地证据先提交，之后才允许可选 observability provider fan-out。
- eval detector 只能写 draft；approved case 必须人工审核。
- provider SDK object、ORM session、credential 和 raw error 不能穿过公共 DTO 边界。

## 更多文档

- [`docs/README.zh-CN.md`](docs/README.zh-CN.md)：复制应用的中文文档地图。
- [`docs/examples.zh-CN.md`](docs/examples.zh-CN.md)：示例 Agent、输入、输出和 eval 命令。
- 源仓库的[架构](../../docs/architecture/README.zh-CN.md)、[扩展指南](../../docs/extension-guide.zh-CN.md)、[adapter 合同](../../docs/adapter-contracts.zh-CN.md)、[context/trust boundary](../../docs/context-and-trust-boundary.zh-CN.md)、[安全策略](../../docs/security-policy.zh-CN.md)、[eval/observability 闭环](../../docs/eval-observability-loop.zh-CN.md)、[发布流程](../../docs/release-process.zh-CN.md)与 [ADR](../../docs/adr/0001-p0-service-boundaries.zh-CN.md)。

复制模板后，仓库级相对链接不会随之存在。请在复制项目自己的 `docs/` 记录应用专属的部署、provider、数据、隐私和恢复决策。

## 许可证和发布边界

核心仓库使用 Apache-2.0。复制后的业务应用应自行选择并记录 license、隐私要求、依赖/SBOM 策略、模型与数据许可证，以及发布流程。

`make dev` 是开发服务器，`make smoke-service` 是端到端验证 harness。它们都不是生产部署，也不能证明 hosted runner、保护环境、artifact service、provider 或 registry 配置已经完成。
