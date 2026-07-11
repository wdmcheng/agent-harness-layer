# ADR-0001：P0 Service Profile 部署边界

- 状态：Accepted
- 日期：2026-07-11

## 背景

local profile 适合离线开发，但无法证明 HTTP producer、durable queue、DBOS workflow、checkpoint 和 event stream 能跨进程恢复。P0 需要可复制的真实部署证明，同时不能把尚未物理拆分的逻辑模块伪装成微服务。

## 决策

1. service profile 使用单一 wheel-only 镜像，以不同 command 启动 migration、FastAPI API 和 runtime worker；PostgreSQL 与 Redis 使用独立容器。
2. API 不执行 executor。RUN-001 先持久化 execution context，再把仅含稳定 refs 的 Pydantic queue DTO 写入 Redis；worker 从 PostgreSQL 恢复身份和输入真相源。
3. worker 使用稳定 DBOS executor id。应用 run owner/workflow 落库后发生硬退出时，替代进程必须先取得 singleton 所有权，再恢复同 DBOS workflow并 `XAUTOCLAIM` 原 Redis entry。
4. CanonicalEvent 使用 PostgreSQL sink；数据库行锁、稳定 event id 和唯一 terminal 约束承担跨 loop/跨进程原子性。进程内 EventBus 不冒充分布式锁。
5. service credential 由隔离 smoke 临时生成，数据库只存 hash；明文不写入仓库、profile、镜像、日志或 artifact。默认 cleanup 删除本轮 container、network、volume、Redis namespace、credential 和临时文件。
6. 未来拆分顺序为：runtime worker（本 ADR 已完成）→ tool/model gateway → observability/event pipeline。storage service 仅在 repository contract 稳定后拆。

## 边界不变量

- 跨边界只传 Pydantic DTO、`CanonicalEvent`、repository/provider/facade interface；禁止传 ORM session、DBOS/provider 原始对象或进程内可变全局。
- queue message 保留 `request_id`、effective `idempotency_key`、`tenant_id`、`run_id`。
- `source_ref`、`trust_level`、context assembly trace、guardrail/audit 和适用 correlation fields 不因拆分丢失。
- approval deny 不创建 continuation；approve 的基础设施失败保留可补投状态，handler 不得重放。

## 后果

- `make smoke-service` 成为 service profile 的验收入口，local smoke 或 mock 不可替代。
- PostgreSQL 暂时仍由 API/worker 通过稳定 repository contract 共享；这不是 storage service 已拆分。
- DBOS 当前采用 singleton worker；需要同 executor id并行扩容时必须引入 Conductor 或新的协调决策。
