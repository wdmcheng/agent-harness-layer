# 让 AI / Agent 操作这个项目

[English](ai-agent-guide.md) | [简体中文](ai-agent-guide.zh-CN.md)

这是一份普通、按需使用的项目指南，不会自动配置 AI 工具，也不会施加目录级指令。你可以把文件链接交给 AI、复制全文，或者明确提示 AI 在操作前先读它。

## 最短使用方式

在复制后的 service-app 项目中，把下面这段发给 AI / Agent：

```text
先阅读 docs/ai-agent-guide.zh-CN.md，再检查当前项目并完成下面的任务。
遵守指南中的真相源、架构、安全、验证和交付约束。
除非我单独授权具体动作，否则不要 commit、push、deploy、publish、使用生产凭据
或调用真实 provider。

任务：<描述你要它做什么>
验收标准：<描述怎样才算完成>
```

范围已经明确时，可以直接使用文末更具体的“初始化项目”或“实现功能”提示词。

## AI 修改前应该先读什么

AI 必须保留当前文件和用户已有改动，然后读取与任务有关的最小文件集：

- `README.zh-CN.md`、`pyproject.toml`、`Makefile`、`.env.example` 和 `.gitignore`；
- 选用的 `configs/profiles/*.yaml`；
- 相关的 `agents/<namespace>/<name>/` package 及 eval case；
- 只有任务跨越边界时才读 `app/api/`、`app/runtime.py` 或 `app/workers/`；
- `tests/` 下的相关测试。

模板仍位于 Agent Harness Layer 源码仓库时，可能还有仓库专属真相源：`Product-Spec.md`、`DEV-PLAN.md`、`API-Contract.md`、active OpenSpec change 或 ADR。这些文件存在时读取与任务直接相关的要求；独立复制项目不得假设它们仍在，也不能因为本指南提到就凭空创建。真相源互相冲突时，必须先报告具体冲突，再修改行为。

## 不可绕过的项目边界

- 修改前把工作拆成小而可独立验证的结果。
- 不覆盖已有项目、Agent 目录或无关用户改动。
- 行为变化先更新适用的用户文档或合同；同时存在英文和 `.zh-CN.md` 指南时保持同步。
- `agents/*` 下业务代码只使用公开 `agent_harness` DTO、protocol、registry、facade 和 repository；不得直接 import vendor SDK、访问 ORM session 或绕过 runtime/policy。
- run、event、usage、audit 和 artifact 证据必须保留 `tenant_id`、`agent_id`、`run_id`；request/trace ID 只能增加关联，不能替代这些身份。
- tool、retrieval、delegation、网络、文件系统和 HITL 只授予需求必需的权限；未使用能力保持关闭。
- 不得在文档、示例、日志、fixture、diff 或交付说明中泄露或提交 secret。
- commit、push、deploy、publish、真实 provider 调用、生产凭据和 registry 副作用都需要用户单独明确授权。
- 必须用实际证据验证改动范围；纯文档只做文档验证，不跑无关全量测试。

## 初始化复制后的 service-app

### 1. 确认核心包来源

先确认当前目录就是目标目录；如果存在 Git，检查 `git status`。判断项目是 Agent Harness Layer workspace 内的模板，还是独立复制项目。

在源码 workspace 内：

```bash
make bootstrap
```

独立复制项目需要受信 `agent-harness` wheel、sdist、源码目录或私有 index。优先显式指定本地来源：

```bash
make bootstrap \
  AGENT_HARNESS_SOURCE=/absolute/path/to/agent_harness-0.1.0-py3-none-any.whl
```

只有用户已经选择并配置受信私有 index 时才使用：

```bash
make bootstrap AGENT_HARNESS_ALLOW_INDEX=1
```

不得静默安装公网上的同名包。没有受信来源时，停止并报告可接受的来源类型。

### 2. 准备 local 状态

除非任务明确需要其他环境，默认使用 local profile 和 fake model。为当前状态库生成 fingerprint key，不打印、不提交其值；然后设置本地路径并迁移：

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

需要持久化本机覆盖项时，可以使用已忽略的 `.env`。不得向 `.env.example` 或 Git 加入默认 fingerprint key；同一个数据库的整个生命周期必须复用同一 key。

### 3. 证明 local 路径可用

```bash
make smoke-local
make run-basic
```

记录实际退出状态和输出的 `run_id`。只有用户要求时才用 `make dev` 启动 HTTP。local/fake 证据不能证明 service profile、外部 telemetry、真实 provider、hosted CI 或生产部署已经验证。

## 实现 Agent 或应用功能

### 1. 把请求转成合同

识别：

- 用户可见行为；
- 类型化输入输出；
- 正常、失败、歧义和安全路径；
- 受影响的公开接口；
- 证明验收所需的证据。

缺失决定会改变公开合同、安全边界、破坏性动作或外部成本时询问用户；其余情况采用并报告最小、可逆假设。

### 2. 把工作映射到五层两翼

| 区域 | 默认决策 |
|---|---|
| 接入层 | 复用 CLI/HTTP、认证、类型化请求、OpenAPI、SSE 和错误信封；只有真实协议需要时才加 route。 |
| 运行时层 | 注册配置并实现 `AgentExecutor`；复用 run、checkpoint、approval、幂等、budget 和 delegation。 |
| 引擎层 | 类型化 schema 和业务行为放在 Agent package 中；通常这是主要实现区域。 |
| 工具层 | 外部动作不是必需时保持 `tool_allowlist` 为空；需要时补类型化 registry、policy、workspace 和 HITL 边界。 |
| 基础设施层 | 先用 local/fake；只有需求出现时才增加 storage、queue、retrieval、provider 或业务 adapter。 |
| 左翼 Eval Gate | 行为先进入 draft，人工审核后才能进入 `approved`；自动化不得自行批准。 |
| 右翼 Observability | 先保留本地 canonical event、usage 和 audit；外部 telemetry 可选且允许降级。 |

未来的 `Graph Nodes`/`GraphState` 和独立 gateway 不是前置。架构图中的概念性 `@agent.tool` 指公共 `ToolRegistry`，不得发明绕过 registry、policy 或 approval 的 decorator。

### 3. 创建或修改 Agent

新建 Agent：

```bash
uv run agent-harness scaffold agent my_team.my_agent
```

项目根自动发现只有 `scaffold agent` 使用。复制应用中的 `agents list` 和 `run` 仍需显式传 `--agents-dir ./agents`：

```bash
uv run agent-harness agents list \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness run my_team.my_agent \
  --profile local \
  --profiles-dir ./configs/profiles \
  --agents-dir ./agents \
  --storage-dsn "$STORAGE_DSN" \
  --prompt '<representative input>'
```

按以下顺序实现：

1. `schemas.py`：类型化输入输出和校验边界；
2. `agent.py`：module-level `executor`，每次只返回一种 `AgentExecutionResult` 状态；
3. `config.yaml`：稳定身份、schema/executor 路径、模型、预算、默认关闭的权限和 approved eval 路径；
4. `tools.py`：只增加需求必需的类型化工具，并补 policy/HITL/workspace 控制；
5. `evals/drafts/`：正常、失败、歧义和安全 case；只有人工审核后才能进入 `approved`。

不要为每个 Agent 手工修改 route 或自建 scheduler。`AgentRegistry.load_from_directory()` 是声明式便捷层，但仍统一执行配置、schema、executor、runtime、policy 和 storage 合同。

### 4. 显式处理跨层改动

- **HTTP/API：** route、schema 和 API 文档一起改；公开 surface 变化时检查 OpenAPI drift。
- **工具或危险动作：** 增加 allow/deny 证据，证明任何副作用之前已经完成 approval。
- **Retrieval/RAG：** 保留 `source_ref`、`trust_level` 和注入边界，覆盖无命中和不可信内容。
- **Storage/queue/service profile：** 增加前滚 migration 和恢复证据；只有改动依赖这些组件时才运行 service smoke。
- **多 Agent：** 声明 `delegation_edges`，使用 registry/policy/shared parent budget，禁止私下递归调用。
- **Provider 集成：** 通过公共 adapter 实现，记录 usage/cost/latency，业务 Agent 保持 vendor-neutral。

### 5. 选择最小充分验证

| 改动 | 最低证据 |
|---|---|
| 纯文档 | 本地链接/锚点、代码块语法和相关文档合同测试；不跑无关全量测试 |
| Agent 行为/schema/config | 定向测试、registry 列表、一个代表性 CLI run 和相关 approved eval |
| Tool/retrieval/policy | 定向 allow/deny/failure/HITL 测试，加本地 event/audit 检查 |
| HTTP/API | 定向 route 测试和 OpenAPI 合同/drift 检查 |
| App/runtime 集成 | `make quality`、定向测试和 `make smoke-local` |
| PostgreSQL/Redis/worker/migration | 相关 migration/integration 测试和 `make smoke-service` |

approved 回归数据使用 `make eval`。`no-approved-cases` 表示没有 approved 证据，不是评测通过。重型 service 或全仓检查必须有范围理由。

## 必须返回的交付说明

AI 应该返回：

1. 结果和用户可见行为；
2. 修改文件及各自原因；
3. 实际验证命令和结果；
4. 未验证环境或副作用；
5. 仍需用户决定的事项。

不得把 local/fake 证据写成 service、hosted、provider、registry 或生产行为已经验证。

## 可复制提示词

### 初始化这个项目

```text
先阅读 docs/ai-agent-guide.zh-CN.md，再初始化这个复制后的 Agent Harness service-app。

受信 agent-harness 来源：<绝对 wheel/sdist/源码路径，或已批准私有 index>
目标 profile：local
初始 Agent：<无或 namespace.name>

保留已有文件，不使用生产凭据，不执行 commit、push、deploy、publish 或真实 provider
调用。安装受信核心包，创建已忽略的 local 状态并迁移，执行 smoke-local 和 run-basic，
然后报告准确命令、结果和阻塞。如果指定了初始 Agent，可以生成骨架，但不得超出我给出
的验收标准自行编造业务行为。
```

### 实现一个功能

```text
先阅读 docs/ai-agent-guide.zh-CN.md，再实现这个功能：

目标：<用户可见结果>
输入/输出：<公开数据合同>
验收标准：<正常、失败和安全行为>
允许访问的外部系统：<无或明确系统>
约束：<兼容性、安全、性能或范围>

先检查当前代码和相关真相源，把改动映射到五层两翼，使用公开 agent_harness seam，
保持最小权限，增加定向回归/eval 证据，只运行最小充分验证。除非我单独授权，否则
不得 commit、push、deploy、publish、使用生产凭据或调用真实 provider。报告修改文件、
准确验证结果、采用的假设和未验证边界。
```
