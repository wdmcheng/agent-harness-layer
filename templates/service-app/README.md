# Agent Harness Service App Template

## What this scaffold is

这是 Agent Harness Layer 的可复制后端应用模板。它把 FastAPI、app-specific Typer 入口、worker、local/service profiles、Docker Compose、eval 目录和测试装配在一起；runtime、policy、approval、eval 与 storage 业务能力仍来自 `agent_harness` 公共 seam。

模板包含 `examples.basic` smoke fixture，以及 RAG assistant、ticket triage、repo analyst、dev assistant 四个 P0 薄样例；不包含 eval experiment，也不提前实施 API/worker 物理拆分。

## Quick Start

仓库内开发时，根 workspace 会把当前 checkout 的核心包注入模板：

```bash
cd templates/service-app
make bootstrap
make dev
```

如果已经把 `templates/service-app` 复制到独立目录，第一次启动必须显式提供本仓库构建的 `agent-harness` wheel、sdist 或源码目录：

```bash
make bootstrap \
  AGENT_HARNESS_SOURCE=/absolute/path/to/agent_harness-0.1.0-py3-none-any.whl
make dev
```

`bootstrap` 会把该可信本地来源写入复制项目自己的 `tool.uv.sources`，后续命令可直接复用。若组织已把 `agent-harness==0.1.0` 发布到可信私有 index，可在配置 `UV_INDEX_URL` 后显式使用 `AGENT_HARNESS_ALLOW_INDEX=1`。独立模板不会默认解析公共同名包；这是供应链边界，不是安装故障。

`make dev` 等价于调用 app-specific 入口：

```bash
uv run agent-harness-service serve \
  --profile local \
  --profiles-dir ./configs/profiles \
  --host 127.0.0.1 \
  --port 8000
```

启动后可访问：

- health：`GET http://127.0.0.1:8000/api/v1/health`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`
- Swagger：`http://127.0.0.1:8000/docs`
- Redoc：`http://127.0.0.1:8000/redoc`

local profile 使用 SQLite、in-memory queue、local JSONL observability 和 fake model，不需要真实 API key 或外部 SaaS provider。自动化启动必须用 `STATE_DIR=<临时目录> make dev`，避免写入开发者已有状态。

运行现有 basic/fake smoke agent 并获得 terminal evidence：

```bash
make run-basic STATE_DIR=/tmp/agent-harness-basic
```

也可以直接使用核心 CLI：

```bash
uv run agent-harness run examples.basic \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents
```

四个示例的真实 run/eval 命令、能力差异和安全边界见 [`docs/examples.md`](docs/examples.md)。快速验证全部 approved fake-model case：

```bash
make eval
```

创建自己的 Agent 时，从 service-app 根目录运行：

```bash
uv run agent-harness scaffold agent support.triage
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents
uv run agent-harness run support.triage \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --prompt '验证 scaffold runtime'
```

省略 `--agents-dir` 时，命令通过当前 service-app 的 `pyproject.toml` 标记定位 `./agents`；无法唯一定位就失败，不猜测相对路径。完整生成、审核和 eval 流程见 [`docs/examples.md`](docs/examples.md#新增自己的-agent)。

service profile 的真实依赖验证使用：

```bash
make smoke-service
```

它验证 Docker Compose PostgreSQL/Redis、migration、repository/UoW 和 queue reachability；health 只做进程与配置摘要，不能替代 service smoke。

## Project Structure

```text
templates/service-app/
├── app/
│   ├── api/                 # HTTP route、DTO 转换和依赖注入
│   ├── cli/                 # app-specific serve，不复制核心 CLI
│   └── workers/             # worker 进程入口
├── agents/examples/         # 每个业务 agent 独立目录
├── configs/profiles/        # local/service 类型化 profile
├── eval-cases/
│   ├── drafts/              # 自动信号只能先进入待审核区
│   └── approved/            # 只能由人工审核流程写入
├── tests/                   # 复制模板后可直接运行的公开 seam 测试
├── docs/                    # app-specific 维护说明入口
├── scripts/                 # 独立 bootstrap 与 service smoke
├── docker-compose.yml
├── .env.example
├── Makefile
└── pyproject.toml
```

## For Agent App Developers

- 业务 agent 放在 `agents/*`，只依赖 `agent_harness` 公共接口，不直接 import vendor SDK。
- 使用 `agent-harness scaffold agent <agent_id>` 生成新目录；`agent_id` 必须是点分小写 Python identifier，例如 `support.triage`。
- `app/*` 只负责协议入口、依赖装配和响应转换，不写业务 agent 逻辑。
- 使用 `make cli ARGS='<核心命令>'` 或 `agent-harness` 执行 agents、run、approvals、eval 和 policy 管理。
- eval detector 只能写 `eval-cases/drafts`；`eval-cases/approved` 必须经过人工审核、policy 和 audit seam。
- 所有运行记录必须保留 `tenant_id`、`agent_id`、`run_id`；delegation 必须经过 registry 和 policy。

## For Scaffold Maintainers

- 核心包 `agent_harness/*` 不得依赖模板 `app`、具体示例 agent 或其配置。
- vendor integration 只能留在受控 adapter 模块，不能反向污染模板、agent 或核心 DTO。
- 模板 CLI 只拥有 `serve`。`doctor`、agents、run、approvals、eval、policy 等命令继续由核心 `agent-harness` 实现。
- 模板自身不得声明 `workspace = true` 或固定 `cd ../..`；仓库内 source 由根 workspace 继承，复制项目由 `make bootstrap` 显式选择本地 artifact 或可信 index。
- `scaffold agent` 属于核心 CLI；模板不得复制生成逻辑。它先在正式 registry 扫描根之外完成渲染和验证，再原子发布具体 Agent 目录。
- 修改 endpoint 必须先更新 `API-Contract.md`，再运行 template/OpenAPI contract tests。

当前 P0 CLI 盘点：

| 能力 | 当前归属 | 状态 |
|---|---|---|
| `doctor` | 核心 CLI | 已有 |
| `agents list` / `run` | 核心 CLI | 已有 |
| `approvals list/approve/deny` | 核心 CLI | 已有 |
| `eval draft/list/approve/run/scores` | 核心 CLI | 已有 |
| `policy check` | 核心 CLI | 已有 |
| `scaffold agent` | 核心 CLI | 已有；原子生成、无 `--force` |
| `serve` | 模板 CLI | 本切片新增 |

## Deep Docs

模板维护入口见 [`docs/README.md`](docs/README.md)。完整 `REQ-018` 深度文档（architecture、extension guide、adapter contracts、eval/observability loop、security、release process 与 ADR）仍由后续文档交付收口；当前入口不冒充完整交付。

## License & Compliance

仓库核心包按 Apache-2.0 发布。应用复制模板后应保留自己的 license/SBOM gate，并确认新增依赖、模型与数据集的许可证和隐私要求；本仓库的完整 `make license-check` 由 release gate 执行。

## Release Process

本模板在源仓库中是 workspace 成员，复制后则是独立项目；两种模式都不代表生产部署已经完成。补齐深度文档后，再按 build、security、license、smoke 和 release gate 发布；不要把 `make dev` 当作生产启动方案。
