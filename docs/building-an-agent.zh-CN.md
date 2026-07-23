# 用五层两翼开发一个 Agent

[English](building-an-agent.md) | [简体中文](building-an-agent.zh-CN.md)

适用读者：第一次使用 Agent Harness Layer 创建业务 Agent 的开发者，以及需要判断功能应该落在哪一层的维护者。

导航：[根 README](../README.zh-CN.md) · [service-app 模板](../templates/service-app/README.zh-CN.md) · [架构图说明](architecture/README.zh-CN.md) · [扩展指南](extension-guide.zh-CN.md) · [示例 Agent](../templates/service-app/docs/examples.zh-CN.md)

这份指南回答一个具体问题：拿到“五层 + 两翼”架构图后，怎样把一个业务想法落成可以运行、评测和观测的 Agent。

五层不是五个都要由业务开发者重写的服务。模板已经提供接入层和运行时层的大部分能力；一个最小 Agent 主要在引擎层实现类型化 schema 和 executor，通过配置声明模型与预算，按需增加工具层和基础设施适配，并让 Eval 与 Observability 两翼贯穿整个生命周期。

## 一张表看懂要改哪里

| 架构区域 | 做一个 Agent 时要做什么 | 主要文件或公共 seam | 最小 Agent 是否必须改 |
|---|---|---|---|
| 1. 接入与交互层 Access | 选择 CLI 或 HTTP 作为入口，复用认证、请求校验、OpenAPI、SSE 和错误信封 | `templates/service-app/app/api/`、核心 `agent-harness` CLI | 否；通常直接复用 |
| 2. 编排与运行时层 Runtime | 让 registry 发现 Agent，由 runtime 管理 run、幂等、checkpoint、resume、审批和 delegation | `config.yaml`、`AgentRegistry`、`RunOrchestrator`、`AgentExecutionResult` | 只需按合同实现 executor/config；不要自建调度器 |
| 3. 引擎与认知层 Engine | 定义类型化输入输出、业务推理步骤、模型选择、预算和可选 delegation | `schemas.py`、`agent.py`、`config.yaml`，以及 model/context 公共 seam | 是；这是业务 Agent 的主体 |
| 4. 工具与能力层 Tools | 为必要能力定义最小工具 schema、allowlist、workspace/policy/HITL 边界 | `tools.py`、`tool_allowlist`、`ToolRegistry`、`WorkspacePolicy`、`PolicyEngine` | 否；只有需要外部动作时才增加 |
| 5. 基础设施与数据层 Infra | 选择 local/service profile，配置 storage、queue、model、retrieval、secret 和业务系统 adapter | `configs/profiles/`、`app/runtime.py`、repository/UoW、provider adapter | local 最小路径无需开发；接真实依赖时配置或扩展 |
| 左翼 Eval Gate | 把可复现行为变成 draft，经人工审核进入 approved，再作为回归和发布证据 | `evals/drafts/`、`evals/approved/`、`EvalRunner`、experiment/acceptance seam | 必须建立最小回归；自动信号不能直接 approved |
| 右翼 Observability | 读取 run/event/usage/audit 证据，先保留本地证据，再按需 fan-out provider | `CanonicalEvent`、events CLI/SSE、local JSONL/PostgreSQL sink、`TelemetryFacade` | 本地证据必须保留；外部 provider 可选 |

架构图中的 `Graph Nodes`、`GraphState`、复杂长期 memory 和独立 tool/model gateway 是目标扩展位，不是创建第一个 Agent 的前置。图中的 `@agent.tool Registry` 是概念性工具注册标签；当前公共 seam 是 `ToolRegistry` 和类型化 descriptor/result DTO，没有可绕过 registry、policy 或 approval 的公共 decorator 语法糖。

## 一次请求如何经过五层两翼

```text
CLI / HTTP
  -> Access：认证、tenant、输入 schema、request/trace ID
  -> Runtime：registry 查找、幂等 run、checkpoint、budget、审批/委派
  -> Engine：executor 读取类型化输入并产生 completed/waiting/failed
  <-> Tools：模型或 executor 只能调用已注册、已授权的能力
  -> Infra：storage、queue、model、retrieval、业务系统

Eval Gate：用同一 Agent 行为运行 approved case，阻止回归越过发布门禁
Observability：每一阶段提交 CanonicalEvent、usage、audit 和安全 evidence
```

两翼不是请求链末尾的两个插件。Eval 从设计输入输出和安全边界时就开始约束行为；Observability 从请求进入时就建立 tenant/Agent/run/request/trace 关联，并贯穿 runtime、tool、model、storage 和结果。所有运行记录及其证据必须保留 `tenant_id`、`agent_id`、`run_id`；request/trace ID 是追加关联，不是替代项。

## 从零创建最小 Agent

### 1. 完成模板初始化

先按 [service-app 首次使用](../templates/service-app/README.zh-CN.md#首次使用local-profile) 完成 bootstrap、fingerprint key、SQLite migration 和 local profile 验证。没有完成 migration 时，`create_app`、CLI run 和 worker 按设计 fail closed。

如果由 AI / Agent 执行，把复制后的模板目录交给它，并明确要求先读普通文档 [AI / Agent 项目操作指南](../templates/service-app/docs/ai-agent-guide.zh-CN.md)。该指南在模板脱离本仓库后仍然存在，包含初始化、实现、验证、授权和交付说明，但不会自动施加目录级规则。

### 2. 生成安全骨架

在 service-app 根目录执行：

```bash
uv run agent-harness scaffold agent support.triage
```

生成结果位于 `agents/support/triage/`，包含 `agent.py`、`schemas.py`、`tools.py`、`config.yaml` 和 draft/approved eval 目录。命令不会覆盖已有目录，也不会自动赋予工具或 delegation 权限。

### 3. 实现引擎层

先在 `schemas.py` 定义输入输出 DTO，再让 `agent.py` 的 module-level `executor` 满足 `AgentExecutor` protocol。每次执行只返回一种合法结果：

- `AgentExecutionResult.completed(output)`：已经产生最终输出；
- `AgentExecutionResult.waiting(approval)`：必须等待 HITL 决议；
- `AgentExecutionResult.failed(error)`：以结构化失败结束。

完整 executor 和 `config.yaml` 示例见 [模板的“创建 Agent”章节](../templates/service-app/README.zh-CN.md#创建-agent)。业务代码不要放入 `app/api`，也不要从 executor 直接读取 profile YAML 或 ORM session。

### 4. 用配置接入 Runtime

`config.yaml` 至少声明：

- 稳定 `agent_id`、版本、名称和说明；
- input/output schema import path；
- `executor: agent:executor`；
- model provider/default model；
- token/cost budget；
- 默认空的 `tool_allowlist` 和 `delegation_edges`；
- approved eval dataset 路径。

这里的声明式注册就是实际便捷层：不需要在 FastAPI route 或 runtime composition 中为每个 Agent 手工接线，`AgentRegistry.load_from_directory()` 会统一校验配置、schema 和 executor。它不会跳过 runtime、policy 或 storage。

### 5. 先从 CLI 验证，再决定是否需要 HTTP

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

复制项目中的 `run` 和 `agents list` 仍需显式传 `--agents-dir ./agents`；项目根自动发现只有 `scaffold agent` 使用。CLI 与 HTTP 最终进入同一 registry、runtime、DTO、storage 和 event seam。先用 CLI 验证业务 Agent，需要应用集成时再使用模板的 [HTTP API](../templates/service-app/README.zh-CN.md#http-api)。

## 什么时候扩展 Tools 和 Infra

| 需求 | 应该增加什么 | 不应该做什么 |
|---|---|---|
| 只做类型化分类、提取或确定性处理 | 保持空 `tool_allowlist`，只实现 schema/executor | 为“以后可能用”预先开放文件、shell 或网络 |
| 读取知识库 | 实现 `RetrievalProvider`，将结果转为带 `source_ref`/`trust_level` 的 `ContextFragment` | 把无来源检索文本直接拼进可信 prompt |
| 读写工作区 | 通过 `ToolRegistry` 注册最小 file tool，并配置 `WorkspacePolicy`/`.agentignore` | 直接调用文件系统绕过路径和 artifact 边界 |
| 执行危险动作 | 增加 policy 与 HITL，返回 `waiting`，通过 approvals CLI/API 决议 | 把公开 resume token 当作批准，或批准前产生副作用 |
| 接真实模型 | 在受控 adapter/integration boundary 实现 provider，并记录 usage/cost/latency | 从业务 Agent import vendor SDK 或透传 raw provider object |
| 多 Agent 协作 | 声明 `delegation_edges`，让 registry、policy 和 shared parent budget 管理委派 | executor 私下递归调用另一个 Agent |
| 进入 service profile | 配置 PostgreSQL、Redis、secret、migration、API/worker，并运行 service smoke | 用 local SQLite 通过冒充跨进程恢复已验证 |

工具和基础设施都应该按需求增加。`tools.py` 存在不代表必须注册工具；profile 能配置 provider 也不代表业务 Agent 可以直接依赖 provider SDK。

## 把两翼接上

### 左翼：先建立可审阅的 Eval

1. 把已验证输入、观察输出和人工确认期望写入 draft；
2. reviewer 检查行为、安全边界和隐私后再批准；
3. `EvalRunner` 只运行 approved case；
4. 新版本通过 optimization/holdout/regression 比较后，仍由 reviewer、policy 和 audit 决定 acceptance。

从最小流程开始：

```bash
make eval
```

详细 draft/approve/run 命令见[示例 Agent 指南](../templates/service-app/docs/examples.zh-CN.md#新增自己的-agent)，experiment 和 acceptance 见 [Eval 与 Observability 闭环](eval-observability-loop.zh-CN.md)。没有 approved case 时，稳定结果是 `no-approved-cases`，不是“评测通过”。

### 右翼：从本地事件看清 Agent 做了什么

运行 Agent 后保留真实 `run_id`，再读取事件：

```bash
uv run agent-harness events stream "$RUN_ID" \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN" \
  --events-path "$STATE_DIR/traces.jsonl"
```

先确认本地 `CanonicalEvent`、usage 和 audit 完整，再接 Logfire、Phoenix 或 Langfuse。外部 provider 失败只能形成 degradation 状态，不能回滚本地证据或让 run 伪装失败/成功。

## 按复杂度选择实现范围

| 目标 | 五层实现范围 | 两翼最低要求 |
|---|---|---|
| 第一个可运行 Agent | 复用 Access/Runtime；实现 Engine；Tools 为空；Infra 用 local/fake | 至少一个人工审核的 approved case；能读取本地事件 |
| 带工具的业务 Agent | 增加 `ToolRegistry`、allowlist、policy/workspace；按风险加入 HITL | eval 覆盖 allow/deny/waiting；事件包含 tool/audit evidence |
| RAG Agent | 增加 retrieval/context assembly 和可信来源标记 | eval 覆盖无命中、低质量与注入内容；观测 retrieval/model usage |
| 多 Agent 工作流 | 增加受 registry/policy/budget 约束的 delegation | eval 覆盖父子预算和失败传播；事件保留 parent/child correlation |
| 生产候选服务 | 切 service profile，完成 migration、认证、API/worker/queue 恢复 | approved regression gate；本地证据先提交，provider fan-out 可降级 |

## 用 `support.triage` 对照七个区域

假设目标是“读取工单文本并给出类型、置信度和是否人工复核”：

1. **Access**：不新增 route，先用 CLI；需要业务系统接入时复用 `POST /api/v1/agents/{agent_id}/runs`。
2. **Runtime**：在 `config.yaml` 注册 `support.triage`，由 registry/runtime 管理 run；低置信度可以输出 `needs_review`，危险动作才使用 approval。
3. **Engine**：`schemas.py` 定义工单输入和分类输出；`agent.py` 实现分类 executor。
4. **Tools**：如果只分类文本，保持空 allowlist；只有查询 CRM 时才增加受控工具。
5. **Infra**：第一个版本使用 local profile/fake model；接真实模型或 CRM 时再实现 adapter 和 secret 配置。
6. **Eval Gate**：为正常、歧义、注入和缺字段输入建立 draft，经人工审核后运行 approved dataset。
7. **Observability**：检查 run/event/usage；对歧义输入记录可解释的结构化结果，不保存原始 secret。

这就是“五层两翼”的实际用法：明确哪些由框架负责、哪些由业务 Agent 实现、哪些能力只有需求出现时才开启，以及两翼如何证明行为可接受。

## 完成标准

- Registry 能列出 Agent，schema/executor/config 全部通过校验。
- CLI 至少完成一个真实 run；需要 HTTP 时再验证同一输入合同。
- 工具、retrieval、delegation 都是最小授权，未使用的能力保持关闭。
- local/service profile 的证据边界没有混写；迁移、队列和 worker 只在需要时引入。
- 至少有人工审核的 approved eval，失败和安全路径有对应 case。
- 能通过 `run_id` 找到事件、usage、audit 与必要 artifact，secret 和 raw provider payload 不越界。
- 扩展只依赖公开 DTO/protocol/registry/facade/repository seam，不直接依赖内部实现或 vendor object。

更细的单项扩展方式见[扩展指南](extension-guide.zh-CN.md)，字段级 HTTP 合同见 [`API-Contract.md`](../API-Contract.md)，实际可运行范例见[四个示例 Agent](../templates/service-app/docs/examples.zh-CN.md)。
