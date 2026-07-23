# Adapter 合同

[English](adapter-contracts.md) | [简体中文](adapter-contracts.zh-CN.md)

适用读者：实现 provider、queue、runtime、storage 或 observability adapter 的 scaffold maintainer；需要判断业务代码可依赖什么的 app developer。

导航：[根 README](../README.zh-CN.md) · [架构边界](architecture/README.zh-CN.md) · [扩展指南](extension-guide.zh-CN.md) · [安全策略](security-policy.zh-CN.md) · [ADR-0002](adr/0002-vendor-adapter-isolation.zh-CN.md)

## 合同层级

| 层级 | 当前公开 seam | 维护边界 |
|---|---|---|
| DTO | `HarnessDTO`、identity/trust DTO、run/queue/event/eval DTO | 可序列化、受校验、无 SDK/ORM object；跨进程保留 tenant/agent/run/request/trace refs |
| Protocol | `ModelProvider`、`RetrievalProvider`、`EmbeddingProvider`、`RunQueue`、`EventSink`/`EventReader`、`TokenVerifier`、`PolicyProvider` | 调用方依赖行为合同，不依赖具体厂商类型 |
| Facade/service | `TelemetryFacade`、model/embedding invocation service、policy/approval/context service | 统一 identity、policy、budget、redaction、local-first evidence 与错误语义 |
| Repository | run、approval、audit、event、eval、retrieval、usage、delegation repositories | 返回/接收 DTO 或 record；隔离 SQLAlchemy query 和并发控制 |
| UoW | `SQLAlchemyUnitOfWork` | transaction/commit/rollback 所有权集中；业务层不持有 `AsyncSession` |
| Adapter | `adapters/models`、`adapters/mcp`、`adapters/queue`、`adapters/runtime`、`adapters/observability`、`storage/adapters` | 相应 vendor SDK 与 driver 的受控边界；ORM 还由 `storage` 内的 model、repository 与 migration 共同拥有 |

公开 seam 以 `packages/agent-harness/src/agent_harness/` 的导出和 protocol 为准。文档列出的是稳定职责，不承诺每个私有 helper 的路径或签名。

## 调用与数据规则

1. 入口把不可信 HTTP/CLI 输入转成校验过的 DTO，并注入服务端 identity；body 不得覆盖 tenant、reviewer 或 permission。
2. service/facade 在副作用前执行权限、policy、budget、approval、workspace 和 capacity 门禁。
3. adapter 只接收完成前置校验的请求，返回 provider-neutral DTO 或封闭错误；raw SDK object 不得越界。
4. durable evidence 先在 repository/UoW 提交，再执行可降级 fan-out；需要 exactly-once 语义时使用 idempotency、claim、lease、fencing 和 outbox，不靠进程内锁。
5. 跨 API/worker queue 只传稳定 refs。worker 从 PostgreSQL 恢复 execution context，不信任 producer 拼出的可变对象。

## 错误与降级

- 输入、权限、policy、workspace、容量和合同错误 fail closed，并在外部副作用前返回结构化错误。
- provider raw exception、credential、response body 和本机绝对路径不得进入 API、event 或 telemetry；只保留封闭 code、有界摘要和安全 evidence refs。
- observability/eval provider fan-out 可降级，但 local DB/event evidence 失败不能伪装成 provider degradation。
- Redis/DBOS/PostgreSQL 的 uncertain outcome 必须进入可恢复或 `needs_review` 状态，不能盲目重放。
- approval deny 不创建 continuation；approve enqueue 失败保留可补投状态，handler 不重放。

## 主要 adapter 边界

### Model、embedding 与 MCP

`ModelProvider`/`EmbeddingProvider` 屏蔽 vendor API；当前 Pydantic AI 和 OpenAI-compatible embedding 实现在 `adapters/models/`。MCP Python SDK 位于 `adapters/mcp/python_sdk.py`，对外保持 `MCPClient`/tool DTO。业务 agent、template API、eval runner 不直接 import 这些 SDK。

### Queue 与 runtime

`RunQueue` 定义 enqueue/receipt/ack/claim 合同；service profile 的 Redis Streams 实现在 `adapters/queue/redis.py`。DBOS 封装在 `adapters/runtime/dbos.py`。稳定 message refs、owner/lease/fencing 和 PostgreSQL checkpoint 是恢复依据，内存对象不是。

### Storage 与 UoW

ORM 的受控实现边界包括 `storage` 下的 model、repository、migration，以及 `storage/adapters/sqlalchemy.py` 中的 engine/UoW。repository 可以封装 SQLAlchemy query 和并发控制，UoW 持有 transaction/commit/rollback；API、worker 和 service 只能组合 repository/UoW，业务 agent、路由 handler 和 provider adapter 不直接操作 session。storage service 仍是未来边界；当前 API/worker 共享 PostgreSQL 不代表已拆服务。

### Event 与 observability

`EventSink`/`EventReader` 提供 local JSONL/PostgreSQL 持久化与授权读取；`TelemetryFacade` 在本地提交后 fan-out OTel/Logfire/Phoenix/Langfuse。SSE 使用同一授权 reader，不新建绕过可见性策略的读取路径。

## Import boundary 证据

```bash
make quality
uv run python scripts/import_boundary_check.py
make test
```

关键合同测试：

- model/provider：`tests/contracts/test_model_usage_invocation_contracts.py`
- retrieval：`tests/contracts/test_retrieval_rag_contracts.py`
- tools/MCP：`tests/contracts/test_tool_registry_public_seam_contracts.py`
- observability：`tests/contracts/test_observability_local_first_fanout_contracts.py`
- queue/runtime：`tests/contracts/test_durable_run_queue_contracts.py`、`tests/integration/test_redis_run_queue_contracts.py`
- storage/event：`tests/contracts/test_postgresql_event_sink_contracts.py`、`tests/contracts/test_usage_execution_authority_contracts.py`

真实 Redis/PostgreSQL/DBOS 只由 `make smoke-service` 和 integration 证据证明，不能用 SQLite mock 替代。

## 常见故障

- import boundary 失败：移动依赖方向，别加 blanket ignore。只有 adapter/integration path 可拥有 vendor import。
- DTO 序列化失败：检查是否夹带 SDK/ORM object、非有限数、绝对路径或过大 payload。
- transaction 状态异常：确认 commit/rollback 属于 UoW，adapter 没有自行提交一半状态。
- provider 降级吞掉主失败：先查 local repository/event 是否提交；主证据失败必须保持失败。
- service 重放重复副作用：核对 idempotency key、claim owner、lease/fencing、outbox 和稳定 call id，不要增加进程内缓存掩盖问题。
