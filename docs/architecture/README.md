# Architecture Diagrams

本目录是项目架构图的固定位置。

## 文件职责

| 图 | 可编辑源 | PNG 预览 | 用途 |
|---|---|---|---|
| 企业级 Pydantic AI 控制论全栈架构 | `pydantic-ai-agent-architecture.drawio` / `pydantic-ai-agent-architecture.excalidraw` | `pydantic-ai-agent-architecture.png` | 产品级全景图，说明 5 层运行链路、Agent Loop、治理面、观测面和 P0 可拆部署边界。 |
| 技术架构图（Agent Harness Layer） | `agent-harness-technical-architecture.drawio` / `agent-harness-technical-architecture.excalidraw` | `agent-harness-technical-architecture.png` | 开发级结构图，说明核心包、template app、DTO、CanonicalEvent、Repository/UoW 和 provider/facade 边界。 |
| 运行链路与信任边界图（Agent Harness Layer） | `agent-harness-runtime-trust-boundaries.drawio` / `agent-harness-runtime-trust-boundaries.excalidraw` | `agent-harness-runtime-trust-boundaries.png` | 运行级链路图，说明 CLI/API、RunOrchestrator、storage/checkpoint、EventBus/artifact 和不可信输入边界。 |
| 部署边界图（Agent Harness Layer） | `agent-harness-deployment-boundaries.drawio` / `agent-harness-deployment-boundaries.excalidraw` | `agent-harness-deployment-boundaries.png` | 部署级边界图，说明 local/service profile、API/worker/PostgreSQL/Redis 协作和未来拆分路径。 |

## 当前部署边界

- local profile 仍是单进程 SQLite/in-memory/local JSONL 开发形态。
- service profile 当前由 PostgreSQL、Redis、migration、FastAPI API 和 runtime worker 组成；API 只认证、校验并排队，worker 持有稳定 DBOS executor id并负责 run/checkpoint/approval continuation。
- API 与 worker 只交换 queue DTO、repository DTO 和 `CanonicalEvent` refs；`source_ref`、`trust_level`、context trace、guardrail/audit、tenant/run/request/trace correlation 必须跨边界保留。
- 未来物理拆分顺序固定为 runtime worker（已拆）→ tool/model gateway → observability/event pipeline；storage service 仅在 repository contract 稳定后再拆。图中的紫色虚线仍表示未来边界，不代表当前 Compose 服务。

## 维护规则

- `.drawio` 是首要可编辑源；`.excalidraw` 用于协作编辑；`.png` 只做审阅预览。
- 修改图语义时，同步更新 `Product-Spec.md` 的“架构依据”、`API-Contract.md` 的上游依据，以及 `DEV-PLAN.md` 中受影响 Phase 的关键文件或状态。
- 图中出现 “待实现 Phase N” 时，Phase 完成后必须同步更新图源和 PNG 预览，不能只更新代码和计划文档。
