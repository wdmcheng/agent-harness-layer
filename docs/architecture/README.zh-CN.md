# 架构与部署边界

[English](README.md) | [简体中文](README.zh-CN.md)

适用读者：需要理解当前运行边界的 app developer，以及维护 package、template 和 adapter 合同的 scaffold maintainer。

导航：[根 README](../../README.zh-CN.md) · [五层两翼开发 Agent](../building-an-agent.zh-CN.md) · [工程原则](../engineering-principles.zh-CN.md) · [扩展指南](../extension-guide.zh-CN.md) · [Adapter 合同](../adapter-contracts.zh-CN.md) · [Context 与信任边界](../context-and-trust-boundary.zh-CN.md) · [安全策略](../security-policy.zh-CN.md) · [ADR](../adr/0001-p0-service-boundaries.zh-CN.md)

本目录是项目架构图的固定位置。图稿用于解释关系，当前行为仍以 `Product-Spec.md`、`API-Contract.md`、公开 DTO/protocol、生产代码和测试为准；图与代码冲突时不能拿图替代证据。

## 文件职责

| 图 | 可编辑源 | PNG 预览 | 用途 |
|---|---|---|---|
| 企业级 Pydantic AI 控制论全栈架构 | `pydantic-ai-agent-architecture.drawio` / `pydantic-ai-agent-architecture.excalidraw` | `pydantic-ai-agent-architecture.png` | 产品级全景图，说明 5 层运行链路、Agent Loop、治理面、观测面和 P0 可拆部署边界。 |
| 技术架构图（Agent Harness Layer） | `agent-harness-technical-architecture.drawio` / `agent-harness-technical-architecture.excalidraw` | `agent-harness-technical-architecture.png` | 开发级结构图，说明核心包、template app、DTO、CanonicalEvent、Repository/UoW 和 provider/facade 边界。 |
| 运行链路与信任边界图（Agent Harness Layer） | `agent-harness-runtime-trust-boundaries.drawio` / `agent-harness-runtime-trust-boundaries.excalidraw` | `agent-harness-runtime-trust-boundaries.png` | 运行级链路图，说明 CLI/API、RunOrchestrator、storage/checkpoint、EventBus/artifact 和不可信输入边界。 |
| 部署边界图（Agent Harness Layer） | `agent-harness-deployment-boundaries.drawio` / `agent-harness-deployment-boundaries.excalidraw` | `agent-harness-deployment-boundaries.png` | 部署级边界图，说明 local/service profile、API/worker/PostgreSQL/Redis 协作和未来拆分路径。 |

## 从五层两翼开始开发

架构图回答“能力属于哪里、边界如何连接”，不等于业务开发者要逐层重写。创建 Agent 的可执行路线、每层需要修改的文件、两翼接入方式和 `support.triage` 完整对照，统一放在[《用五层两翼开发一个 Agent》](../building-an-agent.zh-CN.md)。

阅读产品级全景图时注意两点：

- `Graph Nodes`、`GraphState`、复杂长期 memory 和独立 tool/model gateway 标记为目标或未来扩展位，不是首次创建 Agent 的前置。
- 图中的 `@agent.tool Registry` 是概念性工具注册标签；当前公共接口是类型化 `ToolRegistry` 和 descriptor/result DTO，不存在绕过 registry、policy、workspace 或 approval 的公共 decorator 捷径。

## 当前部署边界

- local profile 仍是单进程 SQLite/in-memory/local JSONL 开发形态。
- service profile 当前由 PostgreSQL、Redis、migration、FastAPI API 和 runtime worker 组成；run create 与 approve continuation 由 API 经 `RunQueue` 分派，查询、校验和 deny 等控制面操作仍在 API 进程完成；worker 持有稳定 DBOS executor id，并负责 run/checkpoint/approve continuation。
- API 与 worker 只交换 queue DTO、repository DTO 和 `CanonicalEvent` refs；`source_ref`、`trust_level`、context trace、guardrail/audit、tenant/run/request/trace correlation 必须跨边界保留。
- 未来物理拆分顺序固定为 runtime worker（已拆）→ tool/model gateway → observability/event pipeline；storage service 仅在 repository contract 稳定后再拆。图中的紫色虚线仍表示未来边界，不代表当前 Compose 服务。

### 当前与未来所有权

| 边界 | 当前事实 | 扩展时必须保持 | 尚未实现 |
|---|---|---|---|
| Access/API | FastAPI/CLI 负责认证注入、请求校验、DTO 转换；service profile 的 API 只 enqueue run | 身份、permission、request/trace/run correlation 进入稳定 DTO | 独立 API gateway 产品 |
| Runtime worker | service profile 已是独立进程；持有 DBOS executor、run/checkpoint 和 approve continuation | queue 只传稳定 refs，恢复以 PostgreSQL 真相为准 | 多 executor 并行协调 |
| Model/tool | 当前在进程内经 provider、registry、policy 和 facade seam 调用 | vendor SDK 只能进入 adapter/integration boundary | 独立 model gateway、tool gateway |
| Event/observability | `CanonicalEvent` 先写 local/PostgreSQL sink，再可选 fan-out provider | provider 失败不能删除本地 evidence；可见性走授权 reader | 独立 event pipeline |
| Storage | API/worker 当前共享 PostgreSQL，local profile 使用 SQLite | 业务层只依赖 repository/UoW，不传 `AsyncSession` | 独立 storage service |

当前拆分决策见 [ADR-0001](../adr/0001-p0-service-boundaries.zh-CN.md)，vendor 隔离见 [ADR-0002](../adr/0002-vendor-adapter-isolation.zh-CN.md)，Redis runtime 与许可证复审边界见 [ADR-0003](../adr/0003-redis-runtime-license-policy.zh-CN.md)，复制模板的离线 API 文档资源见 [ADR-0004](../adr/0004-swagger-ui-offline-assets.zh-CN.md) 与 [ADR-0005](../adr/0005-redoc-offline-assets.zh-CN.md)。

## 验证与证据

```bash
make quality       # format/lint/type/import boundary
make test          # unit、contract、离线 integration
make smoke-local   # SQLite/in-memory/fake model/local JSONL
make smoke-service # 需要 Docker Compose；真实 PostgreSQL/Redis/API/worker
```

`make smoke-service` 才能证明真实 queue、跨进程恢复和 PostgreSQL event evidence；`make smoke-local` 不能替代它。HTTP/SSE 合同证据位于 `tests/contracts/`，真实 Redis/PostgreSQL/DBOS 合同位于 `tests/integration/`，服务级脚本位于 `templates/service-app/scripts/`。

常见故障：图中的边界与代码不一致时先检查图是否仍标有“未来”；API 能 health 但 run 不推进时检查 migration、Redis consumer group 和 worker 日志；local 通过而 service 失败时优先检查 Docker、secret file、PostgreSQL migration 与 Redis namespace，不要把问题降级成 SQLite 验证。

## 维护规则

- `.drawio` 是首要可编辑源；`.excalidraw` 用于协作编辑；`.png` 只做审阅预览。
- 修改图语义时，只更新真正受影响的控制真相源：产品行为变化才改 `Product-Spec.md`，公共 API/CLI/module contract 变化才改 `API-Contract.md`，Phase 所有权、文件、证据或状态变化才改 `DEV-PLAN.md`；不得只为时间戳一致而触碰未受影响契约。
- 图中出现 “待实现 Phase N” 时，Phase 完成后必须同步更新图源和 PNG 预览，不能只更新代码和计划文档。
- 修改边界时先更新对应产品/API/OpenSpec 契约，再改图源并重新导出 PNG；`.png` 不是可编辑真相源。
- 架构或跨模块变更遵循[工程原则](../engineering-principles.zh-CN.md)：先识别不变量与变化轴，代码前更新适用合同，先建失败合同，并把机械可判定规则放入 checker/CI。
